import time
from collections.abc import Callable

import httpx

from ostrovok_scraper.config import ScraperConfig


class OstrovokHttpError(Exception):
    """Ошибка запроса к ostrovok.ru; status_code пуст для таймаутов."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BlockedError(Exception):
    """Сайт блокирует запросы: подряд пришло block_threshold ответов 403/429."""


class OstrovokHttp:
    """Синхронный клиент: пауза между запросами, повторы, остановка при блокировке."""

    def __init__(
        self,
        cfg: ScraperConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._last_attempt: float | None = None
        self._blocked_streak = 0
        self._client = httpx.Client(
            base_url=cfg.base_url,
            timeout=cfg.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": cfg.user_agent, "Accept-Language": "ru"},
            transport=transport,
        )

    def pause(self, seconds: float) -> None:
        self._sleep(seconds)

    def _wait_interval(self) -> None:
        if self._last_attempt is not None:
            wait = self._cfg.min_interval_s - (self._clock() - self._last_attempt)
            if wait > 0:
                self._sleep(wait)
        self._last_attempt = self._clock()

    def request(
        self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None
    ) -> httpx.Response:
        delays = list(self._cfg.retry_delays_s)
        while True:
            self._wait_interval()
            try:
                resp = self._client.request(method, path, params=params, json=json_body)
            except httpx.TransportError as exc:
                # Таймауты и сетевые сбои: повтор по расписанию.
                if not delays:
                    raise OstrovokHttpError(str(exc)[:300]) from exc
                self._sleep(delays.pop(0))
                continue
            except httpx.HTTPError as exc:
                raise OstrovokHttpError(str(exc)[:300]) from exc

            if resp.status_code in (403, 429):
                self._blocked_streak += 1
                if self._blocked_streak >= self._cfg.block_threshold:
                    raise BlockedError(f"{self._blocked_streak} ответов {resp.status_code} подряд")
            else:
                self._blocked_streak = 0

            if resp.is_success:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                if delays:
                    self._sleep(delays.pop(0))
                    continue
            raise OstrovokHttpError(resp.text[:300], status_code=resp.status_code)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OstrovokHttp":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
