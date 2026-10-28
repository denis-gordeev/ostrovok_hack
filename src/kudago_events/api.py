import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Self

import httpx

from kudago_events.config import KudagoConfig

EVENTS_PATH = "/public-api/v1.4/events/"
EVENT_FIELDS = (
    "id,title,slug,site_url,dates,place,categories,tags,age_restriction,price,is_free,favorites_count,"
    "description,body_text"
)


class KudagoApiError(Exception):
    """Ошибка запроса к API KudaGo; status_code пуст для сетевых сбоев."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class KudagoApi:
    """Синхронный клиент публичного API KudaGo: пауза между запросами, повторы на 5xx/429/таймауты."""

    def __init__(
        self,
        cfg: KudagoConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._last_attempt: float | None = None
        # trust_env по умолчанию: HTTPS_PROXY из окружения подхватывается.
        self._client = httpx.Client(
            base_url=cfg.base_url,
            timeout=cfg.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": cfg.user_agent, "Accept": "application/json"},
            transport=transport,
        )

    def events(self, since: datetime, until: datetime) -> Iterator[dict]:
        """События города, актуальные в [since, until), по страницам; ошибка любой страницы → KudagoApiError."""
        url = EVENTS_PATH
        params: dict | None = {
            "location": self._cfg.location,
            "actual_since": int(since.timestamp()),
            "actual_until": int(until.timestamp()),
            "page_size": self._cfg.page_size,
            "order_by": "id",
            "expand": "place,dates",
            "text_format": "text",
            "lang": "ru",
            "fields": EVENT_FIELDS,
        }
        for _ in range(self._cfg.max_pages):
            body = self._get(url, params)
            yield from body.get("results") or []
            url, params = body.get("next"), None
            if not url:
                return
            if httpx.URL(url).host != httpx.URL(self._cfg.base_url).host:
                raise KudagoApiError(f"Ссылка на следующую страницу ведёт на другой сайт: {url}")
        raise KudagoApiError(f"Больше {self._cfg.max_pages} страниц событий — остановлено")

    def _get(self, url: str, params: dict | None) -> dict:
        delays = list(self._cfg.retry_delays_s)
        while True:
            self._wait_interval()
            try:
                resp = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                if not delays:
                    raise KudagoApiError(str(exc)[:300]) from exc
                self._sleep(delays.pop(0))
                continue
            if resp.is_success:
                return resp.json()
            if (resp.status_code == 429 or resp.status_code >= 500) and delays:
                self._sleep(delays.pop(0))
                continue
            if resp.status_code == 403:
                raise KudagoApiError(
                    "403 Forbidden: KudaGo не пускает этот IP (с иностранных IP API отвечает 403). "
                    "Нужен российский IP или HTTPS_PROXY.",
                    status_code=403,
                )
            raise KudagoApiError(f"HTTP {resp.status_code}: {resp.text[:300]}", status_code=resp.status_code)

    def _wait_interval(self) -> None:
        if self._last_attempt is not None:
            wait = self._cfg.min_interval_s - (self._clock() - self._last_attempt)
            if wait > 0:
                self._sleep(wait)
        self._last_attempt = self._clock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
