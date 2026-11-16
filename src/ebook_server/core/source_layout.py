"""Per-source directory layouts that turn folder/file names into identification hints.

Members organise their collections differently (``Author/Title.epub``,
``Last, First/Title.txt``, Calibre's ``Author/Title (id)/file.epub``, flat
``Title - Author.epub`` ...). A :class:`~ebook_server.core.models.SourceLocation`
can therefore declare an ordered list of *path templates*. The first template that
matches a file's path (relative to the source root, without file extension)
supplies author/title/series hints.

Template syntax (segments separated by ``/``):

* ``{author}``  - one author name
* ``{authors}`` - several authors separated by ``&``, ``;``, `` and `` or `` und ``
* ``{title}``   - book title
* ``{series}``  - series name (recorded as evidence only)
* ``{*}``       - any text inside one segment, ignored
* ``**``        - whole path segments (only as a complete segment): zero or more
                  in the middle, one or more at the end (``a/**`` = "inside a")
* anything else is literal text

Examples::

    {author}/**                    # "Austen, Jane/Pride and Prejudice.txt"
    {author}/{title} ({*})/{*}     # Calibre library folders
    {title} - {author}             # flat folder of "Title - Author.epub" files

Hints are *evidence* for the external lookup, never accepted as truth.
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
_PLACEHOLDER = re.compile(r"\{([a-z]+|\*)\}")
_AUTHOR_SPLIT = re.compile(r"\s*(?:&|;|\band\b|\bund\b)\s*", re.IGNORECASE)

NAME_ORDER_AUTO = "auto"
NAME_ORDER_FIRST_LAST = "first_last"
NAME_ORDER_LAST_FIRST = "last_first"
NAME_ORDERS = (NAME_ORDER_AUTO, NAME_ORDER_FIRST_LAST, NAME_ORDER_LAST_FIRST)


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


def _segment_regex(segment: str, seen: set[str], template: str) -> str:
    if "**" in segment:
        raise LayoutTemplateError(f"'**' must be a whole path segment: {template!r}")
    parts: list[str] = []
    position = 0
    for match in _PLACEHOLDER.finditer(segment):
        parts.append(re.escape(segment[position : match.start()]))
        name = match.group(1)
        if name == "*":
            parts.append(r"[^/]*?")
        else:
            if name not in FIELD_NAMES:
                raise LayoutTemplateError(
                    f"unknown placeholder {{{name}}} in {template!r}; "
                    f"allowed: {', '.join('{' + n + '}' for n in FIELD_NAMES)}, {{*}}"
                )
            if name in seen or ({name, *seen} >= {"author", "authors"}):
                raise LayoutTemplateError(f"placeholder {{{name}}} repeated in {template!r}")
            seen.add(name)
            parts.append(rf"(?P<{name}>[^/]+?)")
        position = match.end()
    rest = segment[position:]
    if "{" in rest or "}" in rest:
        raise LayoutTemplateError(f"unbalanced braces in {template!r}")
    parts.append(re.escape(rest))
    return "".join(parts)


@lru_cache(maxsize=256)
def compile_template(template: str) -> re.Pattern[str]:
    """Compile a path template into an anchored regular expression."""
    if not isinstance(template, str) or not template.strip():
        raise LayoutTemplateError("template must be a non-empty string")
    if len(template) > MAX_TEMPLATE_LENGTH:
        raise LayoutTemplateError("template is too long")
    segments = template.strip().strip("/").split("/")
    if any(not segment for segment in segments):
        raise LayoutTemplateError(f"empty path segment in {template!r}")
    seen: set[str] = set()
    out: list[str] = []
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if segment == "**":
            if index == last:
                # Trailing "**" means "inside this folder": one or more segments.
                out.append(r"(?:/[^/]+)+" if index > 0 else r".+")
            else:
                out.append((r"/" if index > 0 else "") + r"(?:[^/]+/)*")
            continue
        previous_was_glob = index > 0 and segments[index - 1] == "**"
        prefix = "/" if index > 0 and not previous_was_glob else ""
        out.append(prefix + _segment_regex(segment, seen, template))
    if not seen:
        raise LayoutTemplateError(f"template captures nothing: {template!r}")
    return re.compile("^" + "".join(out) + "$", re.DOTALL)


def validate_templates(templates: Any) -> list[str]:
    """Validate a list of templates; return a list of error messages."""
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
    if not name:
        return ""
    if order == NAME_ORDER_FIRST_LAST:
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
    path = PurePosixPath(relative_path)
    target = str(path.with_suffix("")) if path.suffix else str(path)
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
            pattern = compile_template(template)
        except LayoutTemplateError:
            continue
        found = pattern.match(target)
        if found is None:
            continue
        groups = found.groupdict()
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
    """Path hints for a file below ``source_location`` or ``None`` when not configured."""
    templates = source_location.path_layouts
    if not templates or not isinstance(templates, list):
        return None
    try:
        root = Path(source_location.root_path).expanduser().resolve()
        relative = Path(source_path).resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    hints = match_path(
        relative.as_posix(),
        [t for t in templates if isinstance(t, str)],
        name_order=source_location.author_name_order,
        ignore_values=source_location.layout_ignore_values
        if isinstance(source_location.layout_ignore_values, list)
        else [],
    )
    return hints
