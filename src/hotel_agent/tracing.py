import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs import LLMResult
from langgraph.types import Command

# Столько символов аргументов tool и текста ошибки видно в терминале; в файл они пишутся целиком.
PREVIEW_CHARS = 200


class AgentTracer(BaseCallbackHandler):
    """Трасса агента по ходам диалога: каждый вызов LLM (время, токены входа, выхода и размышления,
    какие tools она вызвала) и каждый вызов tool (аргументы, время, размер ответа или ошибка).

    Печатает компактно в out; с jsonl_path ещё пишет в файл по JSON-строке на событие
    (type: llm | tool | turn). Подключается callback-ом в config графа, ход обрамляют start_turn/end_turn.
    """

    # Вызывать обработчик прямо в event loop, а не в пуле потоков: строки печатаются по порядку событий.
    run_inline = True

    def __init__(self, *, out: TextIO = sys.stderr, jsonl_path: Path | None = None):
        self.out, self.jsonl_path = out, jsonl_path
        if jsonl_path is not None:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._turn = 0
        self._started: dict[UUID, float] = {}
        self._tools: dict[UUID, tuple[str, Any]] = {}
        self._begin_turn("")

    def start_turn(self, user_text: str) -> None:
        self._turn += 1
        self._begin_turn(user_text)

    def end_turn(self) -> dict:
        """Пишет и печатает итог хода; возвращает его."""
        total = {
            "type": "turn",
            "user": self._user_text,
            "llm_calls": self._llm_calls,
            "tool_calls": self._tool_calls,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "reasoning_tokens": self._reasoning_tokens,
            "duration_s": round(time.monotonic() - self._turn_started, 2),
        }
        self._write(total)
        self._print(
            f"── ход: LLM ×{self._llm_calls}, tools ×{self._tool_calls} · "
            f"{_tokens(self._input_tokens, self._output_tokens, self._reasoning_tokens)} · {total['duration_s']:.1f}s"
        )
        return total

    def _begin_turn(self, user_text: str) -> None:
        self._user_text = user_text
        self._turn_started = time.monotonic()
        self._llm_calls = self._tool_calls = 0
        self._input_tokens = self._output_tokens = 0
        # None, пока провайдер ни разу не сообщил токены размышления: «не сообщил» ≠ «не думал».
        self._reasoning_tokens: int | None = None

    # --- LLM ---

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, **kwargs) -> None:
        self._started[run_id] = time.monotonic()

    def on_llm_start(self, serialized, prompts, *, run_id: UUID, **kwargs) -> None:
        self._started[run_id] = time.monotonic()

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs) -> None:
        self._llm_calls += 1
        duration = self._duration(run_id)
        message = getattr(response.generations[0][0], "message", None)
        usage = (getattr(message, "usage_metadata", None) or {}) if message is not None else {}
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        reasoning = usage.get("output_token_details", {}).get("reasoning")
        tool_calls = [call["name"] for call in getattr(message, "tool_calls", [])]
        model = (getattr(message, "response_metadata", {}) or {}).get("model_name") or (
            response.llm_output or {}
        ).get("model_name")

        self._input_tokens += input_tokens or 0
        self._output_tokens += output_tokens or 0
        if reasoning is not None:
            self._reasoning_tokens = (self._reasoning_tokens or 0) + reasoning

        self._write(
            {
                "type": "llm",
                "n": self._llm_calls,
                "model": model,
                "duration_s": duration,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning,
                "cache_read_tokens": usage.get("input_token_details", {}).get("cache_read"),
                "tool_calls": tool_calls,
            }
        )
        what = ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in Counter(tool_calls).items()) or "ответ"
        label = f"[llm {self._llm_calls}] {model}" if model else f"[llm {self._llm_calls}]"
        self._print(f"{label}  {duration:.1f}s  {_tokens(input_tokens, output_tokens, reasoning)} → {what}")

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        self._llm_calls += 1
        duration = self._duration(run_id)
        self._write({"type": "llm", "n": self._llm_calls, "duration_s": duration, "error": _error(error)})
        self._print(f"[llm {self._llm_calls}] {duration:.1f}s  ошибка: {_preview(_error(error))}")

    # --- tools ---

    def on_tool_start(self, serialized, input_str: str, *, run_id: UUID, inputs: dict | None = None, **kwargs) -> None:
        self._started[run_id] = time.monotonic()
        # inputs — аргументы от LLM без внедрённых зависимостей (runtime); у строкового tool их нет.
        self._tools[run_id] = (serialized.get("name") or kwargs.get("name", "?"), input_str if inputs is None else inputs)

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs) -> None:
        name, args = self._tools.pop(run_id, ("?", None))
        result = _result_text(output)
        if isinstance(output, ToolMessage) and output.status == "error":
            self._tool_done(run_id, name, args, error=result)
        else:
            self._tool_done(run_id, name, args, result_chars=len(result))

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        name, args = self._tools.pop(run_id, ("?", None))
        self._tool_done(run_id, name, args, error=_error(error))

    def _tool_done(self, run_id: UUID, name: str, args: Any, *, result_chars: int = 0, error: str | None = None):
        self._tool_calls += 1
        duration = self._duration(run_id)
        event = {"type": "tool", "name": name, "args": args, "duration_s": duration}
        if error is None:
            event |= {"status": "success", "result_chars": result_chars}
            outcome = f"{_num(result_chars)} симв."
        else:
            event |= {"status": "error", "error": error}
            outcome = f"ошибка: {_preview(error)}"
        self._write(event)
        self._print(f"[tool] {name}  {duration:.1f}s  {_preview(_json(args))} → {outcome}")

    # --- вывод ---

    def _duration(self, run_id: UUID) -> float:
        started = self._started.pop(run_id, None)
        return round(time.monotonic() - started, 2) if started is not None else 0.0

    def _print(self, line: str) -> None:
        print(f"  {line}", file=self.out, flush=True)

    def _write(self, event: dict) -> None:
        if self.jsonl_path is None:
            return
        record = {"time": datetime.now().isoformat(timespec="seconds"), "turn": self._turn, **event}
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(_json(record) + "\n")


def _result_text(output: Any) -> str:
    """Текст, который ответ tool добавит в историю: ToolMessage из Command, ToolMessage или строка."""
    if isinstance(output, Command):
        update = output.update if isinstance(output.update, dict) else {}
        return "".join(m.text for m in update.get("messages", []) if isinstance(m, ToolMessage))
    if isinstance(output, BaseMessage):
        return output.text
    return str(output)


def _tokens(input_tokens: int | None, output_tokens: int | None, reasoning: int | None) -> str:
    return f"in {_num(input_tokens)} · out {_num(output_tokens)} (размышл. {_num(reasoning)})"


def _num(n: int | None) -> str:
    return "н/д" if n is None else f"{n:,}".replace(",", " ")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"
