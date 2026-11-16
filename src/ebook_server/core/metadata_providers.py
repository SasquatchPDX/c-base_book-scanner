"""Additional bibliographic metadata providers.

Each provider returns candidate dicts in the shape ``identification._normalize_candidate``
understands (``key``, ``title``, ``author_name``, ``isbn_10``/``isbn_13``,
``publishers``, ``languages``, ``publish_date``, ``_record_url``), so scoring treats
every provider alike.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode

from defusedxml import ElementTree as ET
from django.conf import settings as django_settings

from .identification import (
    PROVIDER_STATUS_NO_RESULTS,
    PROVIDER_STATUS_RESULTS,
    HttpMetadataProvider,
    ProviderRequestError,
    _normalize_identifier,
)
from .source_layout import normalize_person_name

MAX_RESULTS = 5
_CQL_UNSAFE = re.compile(r'["\\\\]')


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


# --------------------------------------------------------------------------- DNB


_DC = "{http://purl.org/dc/elements/1.1/}"
_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
_SRW_RECORD = ".//{http://www.loc.gov/zing/srw/}record"
_ROLE = re.compile(r"\s*\[[^\]]*\]\s*$")


def clean_dnb_title(raw: str) -> tuple[str, str]:
    """``"[Ende] ; Die unendliche Geschichte : Roman / Michael Ende ; ..."`` → title, subtitle.

    DNB titles carry a bracketed uniform title before ``" ; "``, the statement of
    responsibility after ``" / "``, and the subtitle after ``" : "``.
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
    authors = [v for v in values if re.search(r"\[(Verfasser|Autor)", v, re.IGNORECASE)]
    selected = authors or values
    names: list[str] = []
    for value in selected:
        name = normalize_person_name(_ROLE.sub("", value), "auto")
        if name and name not in names:
            names.append(name)
    return names


def _dnb_publisher(value: str) -> str:
    # "Berlin : Parkum Books" → "Parkum Books"; several are separated by " ; ".
    return value.split(" ; ", 1)[0].rsplit(" : ", 1)[-1].strip()


class DnbMetadataProvider(HttpMetadataProvider):
    """Deutsche Nationalbibliothek catalogue via its public SRU interface (no key needed)."""

    provider_id = "dnb"
    label = "Deutsche Nationalbibliothek"
    endpoint = "https://services.dnb.de/sru/dnb"

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
        language: str | None = None,
    ) -> list[dict[str, Any]]:
        del folder_hint, language
        isbn_error: ProviderRequestError | None = None
        if isbn:
            try:
                hits = self._query(f"num={_CQL_UNSAFE.sub('', isbn)}", f"ISBN {isbn}")
            except ProviderRequestError as exc:
                isbn_error = exc
                hits = []
            if hits:
                return hits
        clauses = []
        if title:
            clauses.append(f'tit="{_CQL_UNSAFE.sub("", title).strip()}"')
        if author:
            clauses.append(f'per="{_CQL_UNSAFE.sub("", author).strip()}"')
        if not clauses:
            if isbn_error is not None:
                raise isbn_error
            return []
        description = " + ".join(
            part
            for part in (
                f'title "{title}"' if title else "",
                f'author "{author}"' if author else "",
            )
            if part
        )
        return self._query(" and ".join(clauses), f"search {description}")

    def _query(self, cql: str, request: str) -> list[dict[str, Any]]:
        params = {
            "version": "1.1",
            "operation": "searchRetrieve",
            "recordSchema": "oai_dc",
            "maximumRecords": str(MAX_RESULTS),
            "query": cql,
        }
        url = f"{self.endpoint}?{urlencode(params, quote_via=quote)}"
        try:
            body = self._get_bytes(url)
            root = ET.fromstring(body)
        except (OSError, ValueError, ET.ParseError) as exc:
            self._request_failed(request, "search", exc)
        records = [self._record(node) for node in root.iterfind(_SRW_RECORD)]
        records = [record for record in records if record.get("title")]
        self._log(
            request,
            PROVIDER_STATUS_RESULTS if records else PROVIDER_STATUS_NO_RESULTS,
            count=len(records),
        )
        return records

    def _record(self, node: Any) -> dict[str, Any]:
        def texts(name: str) -> list[str]:
            return [
                " ".join("".join(el.itertext()).split())
                for el in node.iterfind(f".//{_DC}{name}")
                if "".join(el.itertext()).strip()
            ]

        title, subtitle = clean_dnb_title(next(iter(texts("title")), ""))
        isbns: list[str] = []
        idn = ""
        for element in node.iterfind(f".//{_DC}identifier"):
            kind = str(element.attrib.get(_XSI_TYPE, ""))
            value = "".join(element.itertext()).strip()
            if kind == "tel:ISBN":
                isbns.append(value)
            elif kind == "dnb:IDN":
                idn = value
        isbn10, isbn13 = _isbn_lists(isbns)
        payload: dict[str, Any] = {
            "key": f"dnb:{idn}" if idn else "",
            "title": title,
            "author_name": _dnb_creators(texts("creator")),
            "isbn_10": isbn10,
            "isbn_13": isbn13,
            "publishers": [p for p in (_dnb_publisher(v) for v in texts("publisher")) if p],
            "languages": texts("language"),
            "publish_date": next(iter(texts("date")), ""),
        }
        if subtitle:
            payload["subtitle"] = subtitle
        if idn:
            payload["_record_url"] = f"https://d-nb.info/{idn}"
        return payload


# ------------------------------------------------------------------- Google Books


class GoogleBooksMetadataProvider(HttpMetadataProvider):
    """Google Books volumes API. Requires ``BOOK_SCANNER_GOOGLE_BOOKS_API_KEY``.

    Anonymous use has no quota, so without a key the provider reports itself as
    unavailable instead of failing every request.
    """

    provider_id = "googlebooks"
    label = "Google Books"
    endpoint = "https://www.googleapis.com/books/v1/volumes"

    def __init__(self) -> None:
        super().__init__()
        key = getattr(django_settings, "BOOK_SCANNER_GOOGLE_BOOKS_API_KEY", "")
        if not key:
            raise ValueError("needs an API key (BOOK_SCANNER_GOOGLE_BOOKS_API_KEY)")
        self._api_key = str(key)

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
        language: str | None = None,
    ) -> list[dict[str, Any]]:
        del folder_hint
        isbn_error: ProviderRequestError | None = None
        if isbn:
            try:
                hits = self._query(f"isbn:{isbn}", f"ISBN {isbn}", language=None)
            except ProviderRequestError as exc:
                isbn_error = exc
                hits = []
            if hits:
                return hits
        terms = []
        if title:
            terms.append(f'intitle:"{title.replace(chr(34), "")}"')
        if author:
            terms.append(f'inauthor:"{author.replace(chr(34), "")}"')
        if not terms:
            if isbn_error is not None:
                raise isbn_error
            return []
        description = " + ".join(
            part
            for part in (
                f'title "{title}"' if title else "",
                f'author "{author}"' if author else "",
            )
            if part
        )
        return self._query(" ".join(terms), f"search {description}", language=language)

    def _query(self, q: str, request: str, *, language: str | None) -> list[dict[str, Any]]:
        params = {"q": q, "maxResults": str(MAX_RESULTS), "key": self._api_key}
        if language and len(language) == 2:
            params["langRestrict"] = language
            request += f" (lang={language})"
        url = f"{self.endpoint}?{urlencode(params)}"
        try:
            payload = json.loads(self._get_bytes(url).decode("utf-8"))
        except HTTPError as exc:
            self._request_failed(request, "search", exc)
        except (OSError, ValueError) as exc:
            self._request_failed(request, "search", exc)
        items = payload.get("items", []) if isinstance(payload, dict) else []
        records = [self._record(item) for item in items if isinstance(item, dict)]
        records = [record for record in records if record.get("title")]
        self._log(
            request,
            PROVIDER_STATUS_RESULTS if records else PROVIDER_STATUS_NO_RESULTS,
            count=len(records),
        )
        return records

    def _record(self, item: dict[str, Any]) -> dict[str, Any]:
        raw_info = item.get("volumeInfo")
        info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
        identifiers = [
            str(entry.get("identifier"))
            for entry in info.get("industryIdentifiers") or []
            if isinstance(entry, dict) and str(entry.get("type", "")).startswith("ISBN")
        ]
        isbn10, isbn13 = _isbn_lists(identifiers)
        payload: dict[str, Any] = {
            "key": f"googlebooks:{item.get('id', '')}",
            "title": str(info.get("title") or ""),
            "author_name": [a for a in info.get("authors") or [] if isinstance(a, str)],
            "isbn_10": isbn10,
            "isbn_13": isbn13,
            "publishers": [info["publisher"]] if isinstance(info.get("publisher"), str) else [],
            "languages": [info["language"]] if isinstance(info.get("language"), str) else [],
            "publish_date": str(info.get("publishedDate") or ""),
            "_record_url": str(
                info.get("infoLink") or f"https://books.google.com/books?id={item.get('id', '')}"
            ),
        }
        if isinstance(info.get("subtitle"), str):
            payload["subtitle"] = info["subtitle"]
        return payload
