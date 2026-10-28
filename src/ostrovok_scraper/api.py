import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from ostrovok_scraper.config import KINDS, ScraperConfig
from ostrovok_scraper.http import OstrovokHttp
from ostrovok_scraper.parsing.hotel_page import extract_next_data

SERP_PATH = "/hotel/search/v2/site/serp"
RATES_PATH = "/hotel/search/v1/site/hp/search"
REVIEWS_PATH = "/hotel/search/v1/site/reviews"


@dataclass(frozen=True)
class Fetched:
    """Результат запроса: статус последнего ответа и разобранное тело."""

    status_code: int
    body: dict
    incomplete: bool = False


def _dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


class OstrovokApi:
    """Запросы к внутреннему API ostrovok.ru поверх OstrovokHttp."""

    def __init__(
        self,
        http: OstrovokHttp,
        cfg: ScraperConfig,
        *,
        new_uuid: Callable[[], str] = lambda: str(uuid.uuid4()),
    ):
        self._http = http
        self._cfg = cfg
        self._new_uuid = new_uuid

    def search(self, night: date, paxes: list[dict]) -> Fetched:
        """Поиск по региону: открывает сессию и опрашивает, пока поиск не завершён."""
        session_params = {
            "arrival_date": night.isoformat(),
            "departure_date": (night + timedelta(days=1)).isoformat(),
            "currency": "RUB",
            "language": "ru",
            "paxes": paxes,
            "region_id": self._cfg.region_id,
            "search_uuid": self._new_uuid(),
        }
        resp = self._http.request("GET", SERP_PATH, params={"body": _dumps({"session_params": session_params})})
        info = resp.json()["session_info"]
        session_id = info["session"]["id"]
        region = info["region_related"]["region"]

        payload = {
            "session_params": session_params,
            "page": 1,
            "filters": {"kinds": list(KINDS)},
            "session_id": session_id,
            "map_hotels": True,
        }
        body: dict = {}
        status = resp.status_code
        finished = False
        for _ in range(self._cfg.max_polls):
            self._http.pause(self._cfg.poll_interval_s)
            post = self._http.request("POST", SERP_PATH, params={"session": session_id}, json_body=payload)
            status = post.status_code
            body = post.json()
            if body.get("search_finished"):
                finished = True
                break
        body["session_region"] = region
        return Fetched(status, body, incomplete=not finished)

    def hotel_page(self, region_slug: str, master_id: int, hotel_id: str) -> Fetched:
        """Страница отеля: тело — pageProps из __NEXT_DATA__."""
        resp = self._http.request("GET", f"/hotel/{region_slug}/mid{master_id}/{hotel_id}/")
        return Fetched(resp.status_code, extract_next_data(resp.text))

    def rates(self, hotel_id: str, region_id: int, night: date, paxes: list[dict]) -> Fetched:
        """Тарифы отеля на одну ночь."""
        body = {
            "arrival_date": night.isoformat(),
            "departure_date": (night + timedelta(days=1)).isoformat(),
            "hotel": hotel_id,
            "currency": "RUB",
            "lang": "ru",
            "region_id": region_id,
            "paxes": paxes,
            "search_uuid": self._new_uuid(),
        }
        resp = self._http.request("GET", RATES_PATH, params={"body": _dumps(body)})
        data = resp.json()
        # `rates: null` островок временно отдаёт, когда не ответил поставщик тарифов: такой ответ
        # помечается неполным, чтобы его перезапросил --retry-failed.
        return Fetched(resp.status_code, data, incomplete=isinstance(data, dict) and data.get("rates") is None)

    def reviews(self, hotel_id: str, page: int, page_size: int = 500) -> Fetched:
        """Страница русскоязычных отзывов."""
        params = {"hotel": hotel_id, "lang": "ru", "sort": "relevance_desc", "page": page, "page_size": page_size}
        resp = self._http.request("GET", REVIEWS_PATH, params=params)
        return Fetched(resp.status_code, resp.json())
