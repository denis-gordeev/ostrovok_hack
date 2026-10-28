"""Клиент Overpass API: пауза между запросами, повторы, переключение между зеркалами."""

import time
from collections.abc import Callable

import httpx

from kazan_noise.config import NoiseConfig

# Коды, при которых сервер перегружен и запрос стоит повторить.
_RETRY_STATUSES = (429, 502, 503, 504)
# Коды, при которых это зеркало не ответит (доступ закрыт, нет API, сбой сервера): сразу следующее.
_NEXT_MIRROR_STATUSES = (403, 404, 500)


class OverpassError(Exception):
    """Запрос к Overpass не удался: плохой запрос или исчерпаны все зеркала."""


class _Retry(Exception):
    """Внутренний сигнал: попытку стоит повторить; причина — в тексте."""


class _NextMirror(Exception):
    """Внутренний сигнал: повторять на этом зеркале бесполезно, нужно следующее."""


class OverpassClient:
    """Синхронный клиент Overpass: ≥ min_interval_s между попытками, затем зеркала по очереди."""

    def __init__(
        self,
        cfg: NoiseConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._last_attempt: float | None = None
        self._client = httpx.Client(
            timeout=cfg.timeout_s,
            headers={"User-Agent": cfg.user_agent},
            transport=transport,
        )

    def _wait_interval(self) -> None:
        if self._last_attempt is not None:
            wait = self._cfg.min_interval_s - (self._clock() - self._last_attempt)
            if wait > 0:
                self._sleep(wait)
        self._last_attempt = self._clock()

    def _attempt(self, url: str, ql: str) -> dict:
        """Одна попытка: dict при успехе, _Retry при временном сбое, _NextMirror при отказе зеркала,
        OverpassError при 400."""
        self._wait_interval()
        try:
            resp = self._client.post(url, data={"data": ql})
        except httpx.TransportError as exc:
            raise _Retry(str(exc)[:300]) from exc
        if resp.status_code == 400:
            raise OverpassError(resp.text[:300])
        if resp.status_code in _RETRY_STATUSES:
            raise _Retry(f"HTTP {resp.status_code}")
        if resp.status_code in _NEXT_MIRROR_STATUSES:
            raise _NextMirror(f"HTTP {resp.status_code}")
        if not resp.is_success:
            raise OverpassError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as exc:
            # HTML «server is at capacity» вместо JSON.
            raise _Retry(resp.text[:300]) from exc
        if not isinstance(data, dict):
            raise _Retry(f"ответ не JSON-объект: {resp.text[:300]}")
        if "runtime error" in str(data.get("remark", "")):
            # Таймаут запроса: в ответе может быть неполная выборка, её брать нельзя.
            raise _Retry(str(data["remark"])[:300])
        return data

    def query(self, ql: str) -> dict:
        last = "нет зеркал"
        for url in self._cfg.overpass_urls:
            delays = list(self._cfg.retry_delays_s)
            while True:
                try:
                    return self._attempt(url, ql)
                except _Retry as exc:
                    last = f"{url}: {exc}"
                except _NextMirror as exc:
                    last = f"{url}: {exc}"
                    break
                if not delays:
                    break
                self._sleep(delays.pop(0))
        raise OverpassError(f"все зеркала Overpass исчерпаны, последняя ошибка: {last}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OverpassClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
