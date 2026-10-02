"""Thin, dependency-free client for the VNDB.org "Kana" API.

Docs: https://api.vndb.org/kana
Endpoint: https://api.vndb.org/kana

The API is rate limited (200 requests / 5 minutes, requests taking longer than
3 seconds are aborted), so every call in here is kept small and HTTP 429 / 5xx
responses are retried with a back-off.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

DEFAULT_ENDPOINT = "https://api.vndb.org/kana"
BETA_ENDPOINT = "https://beta.vndb.org/api/kana"
USER_AGENT = "vndb-glossary-builder/1.0 (stdlib urllib)"

#: Fields requested for VN search results / detail views.
VN_FIELDS = (
    "id,title,alttitle,olang,released,languages,"
    "titles{lang,title,latin,main,official}"
)

#: Fields requested for the character cast of a VN.
CHARACTER_FIELDS = (
    "id,name,original,aliases,sex,gender,description,"
    "blood_type,height,weight,bust,waist,hips,cup,age,birthday,"
    "traits{name,group_name,sexual},"
    "vns{id,role,spoiler}"
)

_RETRY_CODES = {429, 500, 502, 503, 504}
_VNDB_ID_RE = re.compile(r"^v\d+$")
_URL_ID_RE = re.compile(r"vndb\.org/[vrp](\d+)", re.IGNORECASE)


class VndbError(RuntimeError):
    """Raised when the API returns an error or cannot be reached."""


def parse_vn_id(text: str) -> str | None:
    """Best-effort extraction of a VN vndbid from user input.

    Accepts ``v17``, ``17``, ``https://vndb.org/v17`` and
    ``https://vndb.org/r123``. Returns a ``v<number>`` string, or ``None`` if
    the input does not look like a visual novel reference.
    """
    text = (text or "").strip()
    if not text:
        return None
    m = _URL_ID_RE.search(text)
    if m:
        return "v" + m.group(1)
    if _VNDB_ID_RE.match(text.lower()):
        return text.lower()
    if text.isdigit():
        return "v" + text
    return None


class VndbClient:
    """Blocking client for the handful of endpoints this tool needs.

    All calls are meant to be issued from a worker thread; see ``gui.py``.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        token: str = "",
        timeout: float = 30.0,
        retries: int = 3,
        min_interval: float = 0.35,
    ) -> None:
        self.endpoint = endpoint.rstrip("/") or DEFAULT_ENDPOINT
        self.token = (token or "").strip()
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self._last_request = 0.0

    # ------------------------------------------------------------------
    # low level
    # ------------------------------------------------------------------
    def _request(self, path: str, payload: dict[str, Any] | None) -> Any:
        url = f"{self.endpoint}/{path.lstrip('/')}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Token {self.token}"

        delay = 2.0
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            gap = self.min_interval - (time.monotonic() - self._last_request)
            if gap > 0:
                time.sleep(gap)
            self._last_request = time.monotonic()

            request = urllib.request.Request(
                url,
                data=data,
                headers=headers,
                method="POST" if data is not None else "GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace").strip()
                if exc.code in _RETRY_CODES and attempt < self.retries:
                    wait = delay
                    retry_after = (exc.headers.get("Retry-After") or "").strip()
                    if retry_after.replace(".", "", 1).isdigit():
                        wait = float(retry_after)
                    time.sleep(wait)
                    delay *= 2
                    continue
                if body:
                    raise VndbError(f"HTTP {exc.code}: {body}") from exc
                raise VndbError(f"HTTP {exc.code}: {exc.reason}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
        raise VndbError(f"Could not reach {url}: {last_error}")

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def query(
        self,
        path: str,
        filters: Any = None,
        fields: str = "",
        results: int = 25,
        page: int = 1,
        sort: str | None = None,
        reverse: bool = False,
        count: bool = False,
    ) -> dict[str, Any]:
        """Issue a single POST query and return the decoded response."""
        payload: dict[str, Any] = {"results": max(1, min(int(results), 100)), "page": int(page)}
        if filters:
            payload["filters"] = filters
        if fields:
            payload["fields"] = fields
        if sort:
            payload["sort"] = sort
        if reverse:
            payload["reverse"] = True
        if count:
            payload["count"] = True
        response = self._request(path, payload)
        if not isinstance(response, dict):
            raise VndbError("Unexpected response from the API.")
        return response

    def paginate(
        self,
        path: str,
        progress: Callable[[int], None] | None = None,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        """Yield every result across pages, using the cheap ``more`` flag."""
        page = 1
        while True:
            response = self.query(path, page=page, **kwargs)
            for item in response.get("results") or []:
                yield item
            if progress is not None:
                progress(len(response.get("results") or []))
            if not response.get("more"):
                return
            page += 1

    # ------------------------------------------------------------------
    # convenience helpers used by the GUI
    # ------------------------------------------------------------------
    def search_vns(self, text: str, results: int = 30) -> list[dict[str, Any]]:
        """Search visual novels by free text or by vndbid.

        ``text`` may be a title, an alias, or a reference such as ``v17`` /
        ``https://vndb.org/v17``.
        """
        text = (text or "").strip()
        if not text:
            raise VndbError("请输入检索关键词或 vndbid。")

        vn_id = parse_vn_id(text)
        if vn_id and (text.lower().startswith("v") or "/" in text or text.isdigit()):
            response = self.query(
                "vn",
                filters=["id", "=", vn_id],
                fields=VN_FIELDS,
                results=results,
            )
            if not response.get("results"):
                raise VndbError(f"未找到 {vn_id}。")
            return response["results"]

        response = self.query(
            "vn",
            filters=["search", "=", text],
            fields=VN_FIELDS,
            results=results,
            sort="searchrank",
        )
        return response.get("results") or []

    def get_vn(self, vn_id: str) -> dict[str, Any]:
        """Fetch a single visual novel entry (accepts loose input)."""
        vn_id = parse_vn_id(vn_id) or (vn_id or "").strip()
        if not vn_id:
            raise VndbError("请输入 vndbid。")
        response = self.query(
            "vn",
            filters=["id", "=", vn_id],
            fields=VN_FIELDS,
            results=1,
        )
        results = response.get("results") or []
        if not results:
            raise VndbError(f"未找到 {vn_id}。")
        return results[0]

    def vn_characters(
        self,
        vn_id: str,
        progress: Callable[[int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch the full character cast of a visual novel.

        Uses ``POST /character`` with a nested ``vn`` filter rather than the
        ``va`` field of ``POST /vn`` because ``va`` only lists *voiced*
        characters and silently drops the rest of the cast.
        """
        vn_id = parse_vn_id(vn_id) or (vn_id or "").strip()
        if not vn_id:
            raise VndbError("请输入 vndbid。")
        characters: list[dict[str, Any]] = []
        for character in self.paginate(
            "character",
            progress=progress,
            filters=["and", ["vn", "=", ["id", "=", vn_id]]],
            fields=CHARACTER_FIELDS,
            results=100,
            sort="id",
        ):
            characters.append(character)
        return characters

    def stats(self) -> dict[str, Any]:
        return self._request("stats", None)