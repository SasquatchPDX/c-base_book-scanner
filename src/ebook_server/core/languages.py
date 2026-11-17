"""Language-code normalisation for comparing metadata from different sources.

Sources disagree on language codes: EPUB/OPF declares BCP 47 / ISO 639-1 (``en``,
``de-DE``), Open Library search returns ISO 639-2/B (``eng``, ``ger``), Open Library
edition records use keys such as ``/languages/ger``, Hardcover returns 639-2 or 639-1,
and Calibre conversions often write ``UND``. Values are normalised to the ISO 639-1
two-letter code when one exists, otherwise to the ISO 639-3 code, *for comparison
only*: stored Book/candidate values keep the provider's own form.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import pycountry

# "undetermined", "no linguistic content", "multiple", "uncoded": no language signal.
NO_SIGNAL_CODES = frozenset({"und", "zxx", "mul", "mis"})
_SUBTAG_SPLIT = re.compile(r"[-_]")
# Longest value worth a name lookup ("Norwegian Bokmål"); bounds work on untrusted input.
_MAX_LANGUAGE_VALUE_LENGTH = 64


@lru_cache(maxsize=1)
def _bibliographic_codes() -> dict[str, Any]:
    """ISO 639-2/B code -> language (``ger`` -> German); pycountry indexes only 639-3."""
    return {
        code: language
        for language in pycountry.languages
        if isinstance(code := getattr(language, "bibliographic", None), str)
    }


@lru_cache(maxsize=1024)
def normalize_language(value: str | None) -> str | None:
    """Return a canonical language code (ISO 639-1, else 639-3) or ``None``.

    ``None`` means "no usable language signal": empty, undetermined/multiple, or
    unrecognised values never count as a mismatch.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > _MAX_LANGUAGE_VALUE_LENGTH:
        return None
    if raw.startswith("/languages/"):
        raw = raw.rsplit("/", 1)[-1]
    primary = _SUBTAG_SPLIT.split(raw.lower(), 1)[0]
    if primary in NO_SIGNAL_CODES:
        return None

    language: Any = None
    if len(primary) == 2 and primary.isalpha():
        language = pycountry.languages.get(alpha_2=primary)
    elif len(primary) == 3 and primary.isalpha():
        language = pycountry.languages.get(alpha_3=primary) or _bibliographic_codes().get(primary)
    elif len(raw) > 3:
        try:
            language = pycountry.languages.lookup(raw)  # full names such as "German"
        except LookupError:
            language = None
    if language is None:
        return None
    code = getattr(language, "alpha_2", None) or getattr(language, "alpha_3", None)
    return code if isinstance(code, str) else None


def normalize_languages(values: Any) -> list[str]:
    """Normalise a string, a list of strings, or a list of ``{"key": ...}`` dicts."""
    if isinstance(values, str | dict):
        values = [values]
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("key")
        code = normalize_language(value) if isinstance(value, str) else None
        if code and code not in result:
            result.append(code)
    return result
