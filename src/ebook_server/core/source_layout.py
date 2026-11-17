"""Per-source folder layouts that turn folder/file names into identification hints.

Members organise collections differently (``Author/Title.epub``, ``Last, First/
Title.txt``, Calibre's ``Author/Title (id)/file.epub``, flat ``Title - Author.epub``).
A :class:`~ebook_server.core.models.SourceLocation` may declare an ordered list of
*path templates*; the first template that matches a file's path (relative to the
source root, file extension stripped) supplies author/title/series hints (#35).

Template syntax (segments separated by ``/``):

* ``{author}``  - one author name
* ``{authors}`` - several authors separated by ``&``, ``;``, `` and `` or `` und ``
* ``{title}``   - book title
* ``{series}``  - series name (recorded as evidence only)
* ``{*}``       - any text inside one segment, ignored
* ``**``        - whole path segments only: zero or more in the middle, one or more
                  at the end (``{author}/**`` = "anything inside an author folder")
* anything else is literal text

Hints are *evidence* for the external lookup, never accepted as truth: they cannot
bypass provider verification or the review thresholds. Sources without templates
behave exactly as before (opt-in per source).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import SourceLocation

FIELD_NAMES = ("author", "authors", "title", "series")
MAX_TEMPLATES = 20
MAX_TEMPLATE_LENGTH = 256
# Matching cost: each path segment is matched on its own (no backtracking across
# segments), but several lazy placeholders inside one segment still backtrack
# polynomially on adversarial names (5 per segment took ~0.25 s on a 255-character
# name, 3 take ~1 ms). Templates are admin-supplied; these caps keep matching cheap.
MAX_PLACEHOLDERS_PER_SEGMENT = 3
MAX_WILDCARDS_PER_TEMPLATE = 4
MAX_GLOBS_PER_TEMPLATE = 2
MAX_SEGMENT_LENGTH = 255
MAX_MATCH_PATH_LENGTH = 1024
_GLOB = "**"
_PLACEHOLDER = re.compile(r"\{([a-z]+|\*)\}")
_AUTHOR_SPLIT = re.compile(r"\s*(?:&|;|\band\b|\bund\b)\s*", re.IGNORECASE)

NAME_ORDER_AUTO = "auto"
NAME_ORDER_FIRST_LAST = "first_last"
NAME_ORDER_LAST_FIRST = "last_first"


class LayoutTemplateError(ValueError):
    """Raised for an invalid path template."""


@dataclass(frozen=True)
class PathHints:
    template: str
    authors: list[str] = field(default_factory=list)
    title: str | None = None
    series: str | None = None

    def is_empty(self) -> bool:
        return not (self.authors or self.title or self.series)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "authors": list(self.authors),
            "title": self.title,
            "series": self.series,
        }


def _segment_regex(segment: str, seen: set[str], template: str) -> re.Pattern[str]:
    if _GLOB in segment:
        raise LayoutTemplateError(f"'**' must be a whole path segment: {template!r}")
    parts: list[str] = []
    position = 0
    placeholders = 0
    for match in _PLACEHOLDER.finditer(segment):
        parts.append(re.escape(segment[position : match.start()]))
        placeholders += 1
        name = match.group(1)
        if name == "*":
            parts.append(r".*?")
        else:
            if name not in FIELD_NAMES:
                allowed = ", ".join("{" + n + "}" for n in FIELD_NAMES)
                raise LayoutTemplateError(
                    f"unknown placeholder {{{name}}} in {template!r}; allowed: {allowed}, {{*}}"
                )
            if name in seen or {name, *seen} >= {"author", "authors"}:
                raise LayoutTemplateError(f"placeholder {{{name}}} repeated in {template!r}")
            seen.add(name)
            parts.append(rf"(?P<{name}>.+?)")
        position = match.end()
    if placeholders > MAX_PLACEHOLDERS_PER_SEGMENT:
        raise LayoutTemplateError(
            f"at most {MAX_PLACEHOLDERS_PER_SEGMENT} placeholders per folder/file name: "
            f"{template!r}"
        )
    rest = segment[position:]
    if "{" in rest or "}" in rest:
        raise LayoutTemplateError(f"unbalanced or unknown braces in {template!r}")
    parts.append(re.escape(rest))
    return re.compile("".join(parts), re.DOTALL)


# A compiled template: one entry per path segment, ``None`` standing for ``**``.
CompiledTemplate = tuple[re.Pattern[str] | None, ...]


@lru_cache(maxsize=256)
def compile_template(template: str) -> CompiledTemplate:
    """Compile a path template into per-segment patterns (``None`` = ``**``)."""
    if not isinstance(template, str) or not template.strip():
        raise LayoutTemplateError("template must be a non-empty string")
    if len(template) > MAX_TEMPLATE_LENGTH:
        raise LayoutTemplateError(f"template is longer than {MAX_TEMPLATE_LENGTH} characters")
    if template.count("{*}") > MAX_WILDCARDS_PER_TEMPLATE:
        raise LayoutTemplateError(
            f"at most {MAX_WILDCARDS_PER_TEMPLATE} {{*}} wildcards per template: {template!r}"
        )
    segments = template.strip().strip("/").split("/")
    if any(not segment for segment in segments):
        raise LayoutTemplateError(f"empty path segment in {template!r}")
    if segments.count(_GLOB) > MAX_GLOBS_PER_TEMPLATE:
        raise LayoutTemplateError(
            f"at most {MAX_GLOBS_PER_TEMPLATE} '**' segments per template: {template!r}"
        )
    seen: set[str] = set()
    compiled = tuple(
        None if segment == _GLOB else _segment_regex(segment, seen, template)
        for segment in segments
    )
    if not seen:
        raise LayoutTemplateError(f"template captures nothing: {template!r}")
    return compiled


def _match_segments(compiled: CompiledTemplate, parts: list[str]) -> dict[str, str] | None:
    """Match path segments against a compiled template; captured groups or ``None``.

    Segments are matched independently, so a failure never re-tries the alternatives
    of earlier segments. ``**`` spans zero or more segments, one or more at the end.
    """
    memo: dict[tuple[int, int], dict[str, str] | None] = {}

    def step(ti: int, pi: int) -> dict[str, str] | None:
        if (ti, pi) in memo:
            return memo[(ti, pi)]
        result: dict[str, str] | None = None
        pattern = compiled[ti] if ti < len(compiled) else None
        if ti == len(compiled):
            result = {} if pi == len(parts) else None
        elif pattern is None:  # "**"
            if ti == len(compiled) - 1:
                result = {} if pi < len(parts) else None
            else:
                for start in range(pi, len(parts) + 1):
                    result = step(ti + 1, start)
                    if result is not None:
                        break
        elif pi < len(parts) and (found := pattern.fullmatch(parts[pi])) is not None:
            rest = step(ti + 1, pi + 1)
            if rest is not None:
                captured = {k: v for k, v in found.groupdict().items() if v is not None}
                result = {**captured, **rest}
        memo[(ti, pi)] = result
        return result

    return step(0, 0)


def validate_templates(templates: Any) -> list[str]:
    """Validate a template list; return error messages (empty when valid)."""
    if templates in (None, ""):
        return []
    if not isinstance(templates, list):
        return ["path layouts must be a JSON list of template strings"]
    if len(templates) > MAX_TEMPLATES:
        return [f"at most {MAX_TEMPLATES} path layouts are allowed"]
    errors: list[str] = []
    for template in templates:
        try:
            compile_template(template)
        except LayoutTemplateError as exc:
            errors.append(str(exc))
    return errors


def _clean(value: str) -> str:
    return " ".join(value.replace("_", " ").split()).strip(" -,")


def normalize_person_name(raw: str, order: str = NAME_ORDER_AUTO) -> str:
    """Return a display-order ("First Last") name from a folder value."""
    name = _clean(raw)
    if not name or order == NAME_ORDER_FIRST_LAST:
        return name
    if name.count(",") == 1:
        last, first = (part.strip() for part in name.split(","))
        return f"{first} {last}".strip() if first and last else (first or last)
    if order == NAME_ORDER_LAST_FIRST and " " in name and "," not in name:
        last, first = name.split(" ", 1)
        return f"{first} {last}"
    return name


def match_path(
    relative_path: str,
    templates: list[str],
    *,
    name_order: str = NAME_ORDER_AUTO,
    ignore_values: list[str] | None = None,
) -> PathHints | None:
    """Apply the first matching template to ``relative_path`` (extension stripped)."""
    if len(relative_path) > MAX_MATCH_PATH_LENGTH:
        return None
    path = PurePosixPath(relative_path)
    target = str(path.with_suffix("")) if path.suffix else str(path)
    parts = [part for part in target.split("/") if part]
    if not parts or any(len(part) > MAX_SEGMENT_LENGTH for part in parts):
        return None
    ignored = {
        _clean(value).casefold() for value in (ignore_values or []) if isinstance(value, str)
    }

    def keep(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _clean(value)
        return cleaned if cleaned and cleaned.casefold() not in ignored else None

    for template in templates:
        try:
            compiled = compile_template(template)
        except LayoutTemplateError:
            continue
        groups = _match_segments(compiled, parts)
        if groups is None:
            continue
        raw_authors: list[str] = []
        if groups.get("author"):
            raw_authors = [groups["author"]]
        elif groups.get("authors"):
            raw_authors = [part for part in _AUTHOR_SPLIT.split(groups["authors"]) if part]
        authors: list[str] = []
        for raw in raw_authors:
            kept = keep(raw)
            if kept is None:
                continue
            name = normalize_person_name(kept, name_order)
            if name and name not in authors:
                authors.append(name)
        hints = PathHints(
            template=template,
            authors=authors,
            title=keep(groups.get("title")),
            series=keep(groups.get("series")),
        )
        if hints.is_empty():
            continue  # everything captured was ignored; let a later template try
        return hints
    return None


def hints_for_source_file(source_location: SourceLocation, source_path: str) -> PathHints | None:
    """Path hints for a file below ``source_location``; ``None`` when not configured."""
    templates = source_location.path_layouts
    if not templates or not isinstance(templates, list):
        return None
    try:
        root = Path(source_location.root_path).expanduser().resolve()
        relative = Path(source_path).resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    ignore_values = source_location.layout_ignore_values
    return match_path(
        relative.as_posix(),
        [template for template in templates if isinstance(template, str)],
        name_order=source_location.author_name_order,
        ignore_values=ignore_values if isinstance(ignore_values, list) else [],
    )
