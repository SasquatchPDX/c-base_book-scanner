"""Language code normalisation and plain-text language detection.

Sources disagree on language codes: EPUB/OPF uses BCP 47 / ISO 639-1 (``en``,
``de-DE``), Open Library uses ISO 639-2/B (``eng``, ``ger``) or keys such as
``/languages/ger``, Calibre writes ``UND``. Everything is normalised to the ISO
639-1 two-letter code when one exists, otherwise to the ISO 639-3 code, so that
values from different sources can be compared.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import pycountry

# "undetermined", "no linguistic content", "multiple", "uncoded" carry no signal.
_NO_SIGNAL = {"und", "zxx", "mul", "mis"}
_SPLIT = re.compile(r"[-_]")

DETECTION_MIN_CHARS = 200
DETECTION_MIN_PROBABILITY = 0.95
DETECTION_SAMPLE_CHARS = 20_000


@lru_cache(maxsize=1024)
def normalize_language(value: str | None) -> str | None:
    """Return a canonical language code (ISO 639-1, else 639-3) or ``None``."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.startswith("/languages/"):
        raw = raw.rsplit("/", 1)[-1]
    lowered = raw.lower()
    primary = _SPLIT.split(lowered, 1)[0]
    if primary in _NO_SIGNAL:
        return None

    language: Any = None
    if len(primary) == 2:
        language = pycountry.languages.get(alpha_2=primary)
    elif len(primary) == 3:
        language = pycountry.languages.get(alpha_3=primary)
        if language is None:
            # ISO 639-2/B bibliographic codes (ger, fre, dut, ...).
            language = next(
                (
                    item
                    for item in pycountry.languages
                    if getattr(item, "bibliographic", None) == primary
                ),
                None,
            )
    if language is None and len(lowered) > 3:
        try:
            language = pycountry.languages.lookup(raw)  # full names such as "German"
        except LookupError:
            language = None
    if language is None:
        return None
    return getattr(language, "alpha_2", None) or getattr(language, "alpha_3", None)


def normalize_languages(values: Any) -> list[str]:
    """Normalise a string, list of strings, or list of ``{"key": ...}`` dicts."""
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


@lru_cache(maxsize=1)
def _identifier() -> Any:
    from py3langid.langid import MODEL_FILE, LanguageIdentifier

    return LanguageIdentifier.from_pickled_model(MODEL_FILE, norm_probs=True)


def detect_language(text: str) -> tuple[str, float] | None:
    """Detect the language of running text; ``None`` when short or uncertain."""
    sample = " ".join(text[:DETECTION_SAMPLE_CHARS].split())
    if len(sample) < DETECTION_MIN_CHARS:
        return None
    code, probability = _identifier().classify(sample)
    probability = float(probability)
    if probability < DETECTION_MIN_PROBABILITY:
        return None
    normalized = normalize_language(str(code))
    return (normalized, probability) if normalized else None
