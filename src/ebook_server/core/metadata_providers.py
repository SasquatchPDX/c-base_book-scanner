"""Optional extra metadata providers: Deutsche Nationalbibliothek and Google Books.

Both are opt-in: list them in ``BOOK_SCANNER_METADATA_PROVIDERS`` (Google Books also
needs ``BOOK_SCANNER_GOOGLE_BOOKS_API_KEY``). Requests go through the shared per-provider
rate limiter. Failures raise :class:`identification.ProviderRequestError` with a short
reason that never contains the request URL, query or API key; an empty list means a
genuine miss. Records use the field names the shared normaliser in identification.py
reads (``key``, ``title``, ``author_name``, ``isbn_10``/``isbn_13``, ``publishers``,
``language``, ``publish_date``, ``subtitle``) plus ``source_url`` for provenance.

Based on the providers proposed in #2 by ij0n.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import structlog
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from django.conf import settings as django_settings

from .identification import (
    DEFAULT_USER_AGENT,
    REQUEST_TIMEOUT_SECONDS,
    ProviderRequestError,
    _normalize_identifier,
    describe_provider_error,
)
from .rate_limit import throttle_provider
from .source_layout import normalize_person_name

logger = structlog.get_logger(__name__)

MAX_RESULTS = 5
# Bound response size: a catalogue answer for five records is a few dozen KiB.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_CQL_UNSAFE = re.compile(r'["\\]')


def _isbn_lists(values: list[str]) -> tuple[list[str], list[str]]:
    isbn10: list[str] = []
    isbn13: list[str] = []
    for value in values:
        normalized = _normalize_identifier(value)
        if normalized is None:
            continue
        target = isbn13 if len(normalized) == 13 else isbn10
        if normalized not in target:
            target.append(normalized)
    return isbn10, isbn13


def _fetch(provider_id: str, url: str, operation: str) -> bytes:
    """GET ``url`` with pacing, timeout and size cap; failures become ProviderRequestError."""
    user_agent = str(
        getattr(django_settings, "BOOK_SCANNER_METADATA_USER_AGENT", "") or DEFAULT_USER_AGENT
    )
    try:
        throttle_provider(provider_id)
        request = Request(url, headers={"User-Agent": user_agent})
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, ValueError) as exc:
        reason = describe_provider_error(exc)
        logger.warning(
            "metadata_provider_request_failed",
            provider_id=provider_id,
            operation=operation,
            error_type=type(exc).__name__,
            reason=reason,
        )
        # ``from None``: the original exception's text can carry the URL (and API key).
        raise ProviderRequestError(reason) from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProviderRequestError("response too large")
    return bytes(body)


def _split_isbn_error(
    lookup: Any,
    isbn: str | None,
) -> tuple[list[dict[str, Any]], ProviderRequestError | None]:
    """Run the ISBN lookup; keep its error to report only if the text search finds nothing."""
    if not isbn:
        return [], None
    try:
        return lookup(isbn), None
    except ProviderRequestError as exc:
        return [], exc


# --------------------------------------------------------------------------- DNB


_DC = "{http://purl.org/dc/elements/1.1/}"
_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
_SRW_RECORD = ".//{http://www.loc.gov/zing/srw/}record"
_ROLE = re.compile(r"\s*\[[^\]]*\]\s*$")


def clean_dnb_title(raw: str) -> tuple[str, str]:
    """``"[Ende] ; Die unendliche Geschichte : Roman / Michael Ende"`` -> title, subtitle.

    DNB titles carry a bracketed uniform title before ``" ; "``, the statement of
    responsibility after ``" / "`` and the subtitle after ``" : "``.
    """
    value = raw.strip()
    while value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            break
        value = value[closing + 1 :].lstrip(" ;")
    value = value.split(" / ", 1)[0].strip()
    title, _, subtitle = value.partition(" : ")
    return title.strip(" ;"), subtitle.strip(" ;")


def _dnb_creators(values: list[str]) -> list[str]:
    authors = [value for value in values if re.search(r"\[(Verfasser|Autor)", value, re.I)]
    names: list[str] = []
    for value in authors or values:
        name = normalize_person_name(_ROLE.sub("", value))
        if name and name not in names:
            names.append(name)
    return names


def _dnb_publisher(value: str) -> str:
    # "Berlin : Parkum Books" -> "Parkum Books"; several are separated by " ; ".
    return value.split(" ; ", 1)[0].rsplit(" : ", 1)[-1].strip()


class DnbMetadataProvider:
    """Deutsche Nationalbibliothek catalogue via its public SRU interface (no key)."""

    provider_id = "dnb"
    endpoint = "https://services.dnb.de/sru/dnb"

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        del folder_hint  # folder names are too noisy for a catalogue query
        hits, isbn_error = _split_isbn_error(
            lambda value: self._query(f"num={_CQL_UNSAFE.sub('', value)}", "isbn"), isbn
        )
        if hits:
            return hits
        clauses = []
        if title and title.strip():
            clauses.append(f'tit="{_CQL_UNSAFE.sub("", title).strip()}"')
        if author and author.strip():
            clauses.append(f'per="{_CQL_UNSAFE.sub("", author).strip()}"')
        if not clauses:
            if isbn_error is not None:
                raise isbn_error
            return []
        records = self._query(" and ".join(clauses), "search")
        if not records and isbn_error is not None:
            raise isbn_error
        return records

    def _query(self, cql: str, operation: str) -> list[dict[str, Any]]:
        params = {
            "version": "1.1",
            "operation": "searchRetrieve",
            "recordSchema": "oai_dc",
            "maximumRecords": str(MAX_RESULTS),
            "query": cql[:512],
        }
        body = _fetch(
            self.provider_id, f"{self.endpoint}?{urlencode(params, quote_via=quote)}", operation
        )
        try:
            root = ET.fromstring(body)
        except (ET.ParseError, DefusedXmlException, ValueError):
            raise ProviderRequestError("malformed response") from None
        records = [self._record(node) for node in root.iterfind(_SRW_RECORD)]
        return [record for record in records if record.get("title")][:MAX_RESULTS]

    def _record(self, node: Any) -> dict[str, Any]:
        def texts(name: str) -> list[str]:
            values = []
            for element in node.iterfind(f".//{_DC}{name}"):
                text = " ".join("".join(element.itertext()).split())
                if text:
                    values.append(text)
            return values

        title, subtitle = clean_dnb_title(next(iter(texts("title")), ""))
        isbns: list[str] = []
        idn = ""
        for element in node.iterfind(f".//{_DC}identifier"):
            kind = str(element.attrib.get(_XSI_TYPE, ""))
            value = "".join(element.itertext()).strip()
            if kind == "tel:ISBN":
                isbns.append(value)
            elif kind == "dnb:IDN" and re.fullmatch(r"[0-9X]{8,12}", value):
                idn = value
        isbn10, isbn13 = _isbn_lists(isbns)
        payload: dict[str, Any] = {
            "title": title,
            "author_name": _dnb_creators(texts("creator")),
            "isbn_10": isbn10,
            "isbn_13": isbn13,
            "publishers": [p for p in (_dnb_publisher(v) for v in texts("publisher")) if p],
            "language": texts("language"),
            "publish_date": next(iter(texts("date")), ""),
        }
        if idn:
            # Records without an IDN get a per-run "result-N" key when saved.
            payload["key"] = f"dnb:{idn}"
            payload["source_url"] = f"https://d-nb.info/{idn}"
        if subtitle:
            payload["subtitle"] = subtitle
        return payload


# ------------------------------------------------------------------- Google Books


class GoogleBooksMetadataProvider:
    """Google Books volumes API; needs ``BOOK_SCANNER_GOOGLE_BOOKS_API_KEY``.

    Anonymous use has almost no quota, so without a key the provider is skipped
    (with a warning) instead of failing every lookup.
    """

    provider_id = "googlebooks"
    endpoint = "https://www.googleapis.com/books/v1/volumes"

    def __init__(self) -> None:
        key = getattr(django_settings, "BOOK_SCANNER_GOOGLE_BOOKS_API_KEY", "")
        if not key:
            raise ValueError("needs an API key (BOOK_SCANNER_GOOGLE_BOOKS_API_KEY)")
        self._api_key = str(key)

    def __repr__(self) -> str:  # never expose the key through repr()
        return f"{type(self).__name__}(endpoint={self.endpoint!r})"

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        del folder_hint
        hits, isbn_error = _split_isbn_error(
            lambda value: self._query(f"isbn:{value}", "isbn"), isbn
        )
        if hits:
            return hits
        terms = []
        if title and title.strip():
            terms.append(f'intitle:"{title.replace(chr(34), "").strip()}"')
        if author and author.strip():
            terms.append(f'inauthor:"{author.replace(chr(34), "").strip()}"')
        if not terms:
            if isbn_error is not None:
                raise isbn_error
            return []
        records = self._query(" ".join(terms), "search")
        if not records and isbn_error is not None:
            raise isbn_error
        return records

    def _query(self, q: str, operation: str) -> list[dict[str, Any]]:
        params = {"q": q[:512], "maxResults": str(MAX_RESULTS), "key": self._api_key}
        body = _fetch(self.provider_id, f"{self.endpoint}?{urlencode(params)}", operation)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProviderRequestError("malformed response") from None
        if not isinstance(payload, dict):
            raise ProviderRequestError("malformed response")
        items = payload.get("items") or []
        if not isinstance(items, list):
            raise ProviderRequestError("malformed response")
        records = [self._record(item) for item in items[:MAX_RESULTS] if isinstance(item, dict)]
        return [record for record in records if record.get("title")]

    def _record(self, item: dict[str, Any]) -> dict[str, Any]:
        raw_info = item.get("volumeInfo")
        info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
        identifiers = [
            str(entry.get("identifier"))
            for entry in info.get("industryIdentifiers") or []
            if isinstance(entry, dict) and str(entry.get("type", "")).startswith("ISBN")
        ]
        isbn10, isbn13 = _isbn_lists(identifiers)
        volume_id = str(item.get("id") or "")
        payload: dict[str, Any] = {
            "title": str(info.get("title") or ""),
            "author_name": [name for name in info.get("authors") or [] if isinstance(name, str)],
            "isbn_10": isbn10,
            "isbn_13": isbn13,
            "publishers": [info["publisher"]] if isinstance(info.get("publisher"), str) else [],
            "language": info["language"] if isinstance(info.get("language"), str) else None,
            "publish_date": str(info.get("publishedDate") or ""),
            "categories": [c for c in info.get("categories") or [] if isinstance(c, str)],
        }
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", volume_id):
            payload["key"] = f"googlebooks:{volume_id}"
            payload["source_url"] = f"https://books.google.com/books?id={volume_id}"
        if isinstance(info.get("subtitle"), str):
            payload["subtitle"] = info["subtitle"]
        return payload
