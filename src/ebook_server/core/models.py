"""Core domain models for Sprint002 foundations."""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, router, transaction
from django.db.models.fields.json import KT
from django.db.models.functions import Lower
from django.utils.timezone import now

from .scan_job_ids import MAX_SCAN_JOB_ID_LENGTH, new_scan_job_id


class BookLinkedRecord(models.Model):
    """Serialize book reassignment and retain the old parent for search signals (#158).

    The read and UPDATE must share a row lock: two simultaneous A→B and A→C
    edits must invalidate B as well as A/C. No extra lookup is needed when the
    caller explicitly saves fields other than the book relation.
    """

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Preserve Django's positional-save compatibility as well as keyword calls.
        fields = kwargs.get("update_fields", args[3] if len(args) > 3 else None)
        if fields is not None:
            # update_fields may be a one-shot iterable; do not consume it before
            # Model.save gets to validate and apply it.
            fields = frozenset(fields)
            if "update_fields" in kwargs:
                kwargs["update_fields"] = fields
            elif len(args) > 3:
                args = (*args[:3], fields, *args[4:])
        if self.pk is None or (fields is not None and not {"book", "book_id"}.intersection(fields)):
            super().save(*args, **kwargs)
            return
        using = kwargs.get("using", args[2] if len(args) > 2 else None) or router.db_for_write(
            type(self), instance=self
        )
        with transaction.atomic(using=using):
            previous = (
                type(self)
                ._base_manager.using(using)
                .select_for_update()
                .filter(pk=self.pk)
                .values_list("book_id", flat=True)
                .first()
            )
            self.__dict__["_search_previous_book_id"] = previous
            try:
                super().save(*args, **kwargs)
            finally:
                self.__dict__.pop("_search_previous_book_id", None)


class UserRole(models.TextChoices):
    NORMAL_USER = "normal", "Normal user"
    ADMIN = "admin", "Admin"


DEFAULT_USER_ROLE: Final[str] = "normal"


class AppUserProfile(models.Model):
    """LDAP-backed identity metadata tracked locally."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="library_profile",
    )
    ldap_uid = models.CharField(max_length=255, unique=True)
    role = models.CharField(max_length=16, choices=UserRole.choices, default=UserRole.NORMAL_USER)
    is_disabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "App user profile"
        verbose_name_plural = "App user profiles"
        constraints = [
            models.UniqueConstraint(fields=["ldap_uid"], name="uniq_appuserprofile_ldap_uid"),
        ]

    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def __str__(self) -> str:
        return f"{self.ldap_uid} ({self.role})"


class Author(models.Model):
    """Canonical author entity separated from free-form uploader metadata."""

    full_name = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["full_name"]

    def __str__(self) -> str:
        return self.full_name


class Category(models.Model):
    """Library shelving bucket, curated by admins only.

    Categories are *not* derived from subject tags; tag-derived classification goes to
    ``Genre`` (#50, see docs/metadata-providers.md).
    """

    name = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "categories"

    def __str__(self) -> str:
        return self.name


def genre_key(value: str) -> str:
    """Comparison key for genre names and aliases.

    Folds case, accents, ``&``/``and``, punctuation and whitespace, so "Sci-Fi", "SciFi"
    and "sci fi" share one key. Only exact keys match; there is no fuzzy matching.
    """
    text = unicodedata.normalize("NFKD", value).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("&", " and ")
    return re.sub(r"[\W_]+", "", text)


class Genre(models.Model):
    """Literary genre (fiction or nonfiction) assigned from subject tags (#50).

    Only admins create genres; tags that map to no genre or alias stay evidence.
    """

    name = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Ownership marker for the 0023 starter-taxonomy migration: only rows it created are
    # removed when it is reversed, and only while unused. Admin-created rows stay False.
    seeded = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering: ClassVar[list[str]] = ["name"]

    def __str__(self) -> str:
        return self.name


class GenreAlias(models.Model):
    """Admin-editable alternative spelling of a genre ("SciFi" -> Science fiction)."""

    genre = models.ForeignKey(Genre, on_delete=models.CASCADE, related_name="aliases")
    alias = models.CharField(
        max_length=255,
        help_text=(
            "Subject tag as providers or files write it. Case, accents, punctuation and "
            "spacing are ignored when matching."
        ),
    )
    key = models.CharField(max_length=255, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    # Ownership marker for the 0023 starter-taxonomy migration: only rows it created are
    # removed when it is reversed, and only while unused. Admin-created rows stay False.
    seeded = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering: ClassVar[list[str]] = ["alias"]
        verbose_name_plural = "genre aliases"

    def clean(self) -> None:
        key = genre_key(self.alias)
        if not key:
            raise ValidationError({"alias": "Alias needs at least one letter or digit."})
        if GenreAlias.objects.filter(key=key).exclude(pk=self.pk).exists():
            raise ValidationError({"alias": "An equivalent alias already exists."})
        for genre in Genre.objects.exclude(pk=self.genre_id).only("id", "name"):
            if genre_key(genre.name) == key:
                raise ValidationError({"alias": f"This is the name of the genre {genre.name}."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.key = genre_key(self.alias)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.alias} -> {self.genre_id}"


class BookGenre(BookLinkedRecord):
    """Book-to-genre link with provenance (#50).

    Uses the original auto-created ``core_book_genres`` table. Automatic assignment never
    removes or downgrades reviewer- or admin-set rows.
    """

    class Source(models.TextChoices):
        PROVIDER = "provider", "Metadata provider subject"
        EMBEDDED = "embedded", "Embedded file subject"
        REVIEWER = "reviewer", "Reviewer"
        ADMIN = "admin", "Admin"

    book = models.ForeignKey("Book", on_delete=models.CASCADE)
    genre = models.ForeignKey(Genre, on_delete=models.CASCADE)
    # Rows added without explicit provenance (the admin inline, ``book.genres.add``)
    # are treated as hand-curated.
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.ADMIN)
    source_tag = models.CharField(
        max_length=255,
        blank=True,
        help_text="Subject tag that mapped to this genre, for automatic assignments.",
    )
    created_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "core_book_genres"
        unique_together = (("book", "genre"),)

    def __str__(self) -> str:
        return f"{self.book_id}:{self.genre_id} ({self.source})"


class StorageLocation(models.Model):
    """Abstract logical location for archive/artifacts."""

    class BackendType(models.TextChoices):
        LOCAL = "local", "Local filesystem"
        NFS = "nfs", "NFS share"
        SAMBA = "samba", "SMB/Samba share"

    identifier = models.SlugField(max_length=64, unique=True)
    backend = models.CharField(
        max_length=16,
        choices=BackendType.choices,
        default=BackendType.LOCAL,
    )
    base_path = models.CharField(
        max_length=1024,
        help_text="Logical mount/path prefix, implementation specific",
    )
    read_only = models.BooleanField(
        default=False,
        help_text=(
            "The application only reads from this location (files indexed in place). "
            "It never writes, moves or deletes files here; derived artifacts go to the "
            "default archive location instead."
        ),
    )
    sentinel_file = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "Read-only locations: a file name, relative to the base path, that must exist "
            "for the share to count as mounted (for example .ebook-index-sentinel). While "
            "it is absent, drift checks report a storage outage and flag nothing. Left "
            "empty, an empty base directory (a bare mount point) counts as unmounted."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.identifier


MAX_PUBLICATION_YEAR = 9999


def clean_publication_year(value: object) -> int | None:
    """Return a plausible publication year (1-9999) or ``None``."""
    if isinstance(value, bool):
        return None
    try:
        year = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return year if 1 <= year <= MAX_PUBLICATION_YEAR else None


class SourceLocation(models.Model):
    """Configured input source for ingestion workflows."""

    class SourceType(models.TextChoices):
        INCOMING_FOLDER = "incoming_folder", "Incoming folder"
        NETWORK_SOURCE = "network_source", "Network source"

    class TransferMode(models.TextChoices):
        COPY = "copy", "Copy into archive (keep source)"
        MOVE = "move", "Move into archive (delete source after verified copy)"
        INDEX_IN_PLACE = "index", "Index in place (no copy; source stays read-only)"

    name = models.CharField(max_length=128, unique=True)
    root_path = models.CharField(max_length=1024)
    transfer_mode = models.CharField(
        max_length=8,
        choices=TransferMode.choices,
        default=TransferMode.COPY,
    )
    index_storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="indexed_sources",
        help_text=(
            "Index in place only: the read-only storage location the files are referenced "
            "through. Its base path must contain this source's root path. Left empty, one "
            "is created from the root path on first import."
        ),
    )
    is_active = models.BooleanField(default=True)
    recursive = models.BooleanField(default=True)
    default_source_type = models.CharField(
        max_length=16,
        choices=SourceType.choices,
        default=SourceType.NETWORK_SOURCE,
    )
    default_metadata = models.JSONField(default=dict, blank=True)
    is_paused = models.BooleanField(
        default=False,
        help_text=(
            "Paused sources queue no new imports, and queued imports that have not "
            "started stay DISCOVERED until the source is resumed."
        ),
    )
    ingest_batch_size = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Maximum import jobs in flight for this source. Blank uses "
            "BOOK_SCANNER_INGEST_BATCH_SIZE; 0 means unlimited."
        ),
    )
    ingest_max_per_hour = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Maximum new import jobs queued per rolling hour for this source. Blank "
            "uses BOOK_SCANNER_INGEST_MAX_PER_HOUR; 0 means unlimited."
        ),
    )

    class AuthorNameOrder(models.TextChoices):
        AUTO = "auto", 'Auto ("Last, First" when a comma is present)'
        FIRST_LAST = "first_last", "First Last (never reorder)"
        LAST_FIRST = "last_first", "Last First (reorder even without a comma)"

    # Folder layout: opt-in per source, because not every folder names an author (#35).
    path_layouts = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Ordered list of path templates relative to the source root (file extension "
            "stripped); the first match supplies author/title identification hints. "
            "Placeholders: {author}, {authors}, {title}, {series}, {*}; '**' = any "
            'folders. Examples: ["{author}/**"], ["{author}/{title} ({*})/{*}"], '
            '["{title} - {author}"]. Leave empty when the collection has no consistent '
            "structure."
        ),
    )
    author_name_order = models.CharField(
        max_length=16,
        choices=AuthorNameOrder.choices,
        default=AuthorNameOrder.AUTO,
    )
    layout_ignore_values = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Captured folder/file values that are not names, e.g. ["Unknown", "Misc"]. '
            "Compared case-insensitively; an ignored value yields no hint."
        ),
    )
    # Opt-in (#39): generic same-name pairs ("notes.txt"/"notes.pdf") are not one book
    # in every collection.
    group_sibling_formats = models.BooleanField(
        default=False,
        help_text=(
            "Treat files in the same folder with the same name and different extensions "
            "(e.g. 'Dune.epub', 'Dune.mobi') as formats of one book: once one is linked "
            "to a book, the others are offered that book as a strong candidate. They are "
            "still looked up externally, and conflicting evidence goes to review."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def _clean_index_storage_location(self) -> None:
        from django.core.exceptions import ValidationError

        location = self.index_storage_location
        if location is None:
            return
        if self.transfer_mode != self.TransferMode.INDEX_IN_PLACE:
            raise ValidationError(
                {"index_storage_location": "Only used by the index-in-place transfer mode."},
            )
        if not location.read_only:
            raise ValidationError(
                {"index_storage_location": "Index-in-place storage locations must be read-only."},
            )
        root = Path(self.root_path).expanduser().resolve()
        base = Path(location.base_path).expanduser().resolve()
        if root != base and base not in root.parents:
            raise ValidationError(
                {"index_storage_location": "The source root path must lie inside its base path."},
            )

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        from .source_layout import validate_templates

        errors = validate_templates(self.path_layouts)
        if errors:
            raise ValidationError({"path_layouts": errors})
        if not isinstance(self.layout_ignore_values, list) or not all(
            isinstance(value, str) for value in self.layout_ignore_values
        ):
            raise ValidationError({"layout_ignore_values": "Must be a JSON list of strings."})
        self._clean_index_storage_location()


class Book(models.Model):
    """Canonical bibliographic entity."""

    title = models.CharField(max_length=255)
    subtitle = models.CharField(max_length=512, blank=True)
    publisher = models.CharField(max_length=255, blank=True)
    # Providers only give a (first-)publication *year*; storing it as 1 January would
    # fabricate day precision (#38). ``publication_date`` holds a genuinely known full
    # date only (for example entered by an admin) and is empty otherwise.
    publication_year = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_PUBLICATION_YEAR)],
    )
    publication_date = models.DateField(
        null=True,
        blank=True,
        help_text="Full publication date, only when the exact day is known.",
    )
    edition = models.CharField(max_length=128, blank=True)
    language = models.CharField(max_length=32, blank=True)
    description = models.TextField(blank=True)
    # Folder, relative to a writable archive location, that holds this book's files
    # under a book-scoped archive layout (for example ``Author/Title``). It is assigned
    # when the first file is archived and kept when the title or author is corrected
    # later, so existing files stay where they are; only an explicit reorganize moves
    # them (see core.archive_layout). Empty until assigned and under the ``job`` layout.
    archive_folder = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def published_year(self) -> int | None:
        """The publication year, whichever precision is stored."""
        if self.publication_year is not None:
            return self.publication_year
        return self.publication_date.year if self.publication_date else None

    authors: models.ManyToManyField[Author, Author] = models.ManyToManyField(
        "Author",
        related_name="books",
        blank=True,
    )
    categories: models.ManyToManyField[Category, Category] = models.ManyToManyField(
        "Category",
        related_name="books",
        blank=True,
    )
    genres: models.ManyToManyField[Genre, BookGenre] = models.ManyToManyField(
        "Genre",
        related_name="books",
        blank=True,
        through="BookGenre",
    )
    # Set on any reviewer or admin genre decision, including "no genres" (#50). Automatic
    # assignment and the ``assign_genres`` backfill never touch a curated Book, whether
    # or not it has genre links.
    genres_curated_at = models.DateTimeField(null=True, blank=True, editable=False)
    genres_curated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
        on_delete=models.SET_NULL,
    )

    front_cover = models.ForeignKey(
        "CoverImage",
        null=True,
        blank=True,
        related_name="front_for",
        on_delete=models.SET_NULL,
    )
    back_cover = models.ForeignKey(
        "CoverImage",
        null=True,
        blank=True,
        related_name="back_for",
        on_delete=models.SET_NULL,
    )

    class Meta:
        ordering = ["title"]
        indexes = [
            # Newest-first browsing and the "Recently added" shelf without a sort (#28).
            models.Index(fields=["-created_at", "-id"], name="book_created_idx"),
        ]
        constraints = [
            # Case-insensitive, because SMB shares and Windows/macOS clients treat
            # "Asimov/Foundation" and "asimov/foundation" as the same folder.
            models.UniqueConstraint(
                Lower("archive_folder"),
                condition=~models.Q(archive_folder=""),
                name="uniq_book_archive_folder_ci",
            ),
        ]

    def __str__(self) -> str:
        return self.title


class BookOpfState(models.Model):
    """Regeneration state of a book folder's generated ``metadata.opf`` (see core.book_opf).

    Kept out of ``Book`` on purpose: an ordinary ``Book.save()`` writes every column of
    a possibly stale instance, and must never roll this state back. Both counters only
    move through queryset updates. The OPF is stale while ``requested_generation`` is
    ahead of ``written_generation``.
    """

    book = models.OneToOneField(
        Book, on_delete=models.CASCADE, primary_key=True, related_name="opf_state"
    )
    # Incremented (F() + 1) on every metadata change that affects the OPF.
    requested_generation = models.PositiveBigIntegerField(default=0)
    # The requested generation the worker last rendered and wrote, set only while that
    # generation is still the requested one.
    written_generation = models.PositiveBigIntegerField(default=0)
    # Set when a job was queued and not yet started; the job clears it when it starts.
    # Dedupes queued jobs per book; a claim older than the job timeout is ignored.
    job_queued_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.book_id}: {self.written_generation}/{self.requested_generation}"


class Identifier(BookLinkedRecord):
    """Identifiers associated with a book (ISBN-10, ISBN-13, DOI, etc.)."""

    class IdentifierType(models.TextChoices):
        ISBN10 = "isbn10", "ISBN-10"
        ISBN13 = "isbn13", "ISBN-13"
        DOI = "doi", "DOI"
        OCLC = "oclc", "OCLC"
        OTHER = "other", "Other"

    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="identifiers")
    kind = models.CharField(max_length=16, choices=IdentifierType.choices)
    value = models.CharField(max_length=255)
    normalized_value = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["book", "kind", "value"],
                name="uniq_book_identifier_kind_value",
            )
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


class BookSearchDocument(models.Model):
    """Denormalised, rebuildable search/browse row for one Book (#28).

    Never a source of truth: `ebook_server.core.search` rewrites it from the
    Book, its authors, identifiers, genres, categories and COMPLETE variants,
    and `manage.py rebuild_search_index` can recreate every row. Text columns
    hold accent- and case-folded text, so trigram and keyset queries need no
    per-row function call. `document` is a PostgreSQL `tsvector`; it stays
    NULL on other databases, which use the plain `icontains` fallback.
    """

    book = models.OneToOneField(
        Book,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="search_document",
    )
    document = SearchVectorField(null=True, blank=True)
    title_sort = models.CharField(max_length=255, blank=True)
    author_sort = models.CharField(max_length=255, blank=True)
    author_text = models.TextField(blank=True)
    language = models.CharField(max_length=32, blank=True)
    publication_decade = models.SmallIntegerField(null=True, blank=True)
    has_cover = models.BooleanField(default=False)
    book_created_at = models.DateTimeField()
    # SHA-256 of the weighted full-text input, so drift in text that only reaches
    # the `tsvector` (description, publisher, identifiers) stays detectable.
    content_digest = models.CharField(max_length=64, blank=True)
    indexed_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["-book_created_at", "-book"],
                name="booksearch_created_idx",
            ),
            models.Index(fields=["title_sort", "book"], name="booksearch_title_idx"),
            models.Index(fields=["author_sort", "book"], name="booksearch_author_idx"),
            models.Index(fields=["language", "book"], name="booksearch_language_idx"),
            # PostgreSQL only; migration 0034 skips these on other databases.
            GinIndex(fields=["document"], name="booksearch_document_gin"),
            GinIndex(
                fields=["title_sort"],
                opclasses=["gin_trgm_ops"],
                name="booksearch_title_trgm",
            ),
            GinIndex(
                fields=["author_text"],
                opclasses=["gin_trgm_ops"],
                name="booksearch_author_trgm",
            ),
        ]

    def __str__(self) -> str:
        return f"search document for book {self.book_id}"


class BookSearchFacet(models.Model):
    """One facet value of a Book's search document (#28).

    A narrow `(kind, value, book)` table instead of array columns: it is
    B-tree indexed on every database, filters are exact equality on a
    normalised value (no `UPPER(format)` misestimate), and facet counts are a
    `GROUP BY value`. Genre, category and author facets store the related
    row's id; format stores the lower-cased format code. Language and
    publication decade are single-valued, so they are columns of the
    document instead.
    """

    class Kind(models.TextChoices):
        FORMAT = "format", "Format"
        AUTHOR = "author", "Author"
        GENRE = "genre", "Genre"
        CATEGORY = "category", "Category"

    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="search_facets")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    value = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["book", "kind", "value"],
                name="uniq_booksearchfacet_book_kind_value",
            ),
        ]
        indexes = [
            models.Index(fields=["kind", "value", "book"], name="booksearchfacet_lookup_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind}={self.value}"


class CoverImage(BookLinkedRecord):
    """Stored front/back cover image per book."""

    class CoverPosition(models.TextChoices):
        FRONT = "front", "Front"
        BACK = "back", "Back"

    class Source(models.TextChoices):
        """Where the cover came from (Sprint020 G: every cover records its origin)."""

        EXTRACTED = "extracted", "Extracted from a book file"
        SIDECAR = "sidecar", "Sidecar image next to the book file"
        SCANNER = "scanner", "Photographed by the scanner"

    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="cover_images")
    position = models.CharField(max_length=16, choices=CoverPosition.choices)
    storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.PROTECT,
        related_name="cover_images",
    )
    storage_identifier = models.CharField(max_length=512)
    mime_type = models.CharField(max_length=128)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    source_variant = models.ForeignKey(
        "FileVariant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="extracted_covers",
    )
    extractor_tool = models.CharField(max_length=128, blank=True)
    extractor_version = models.CharField(max_length=64, blank=True)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.EXTRACTED)
    # The scanner cover version this cover was made from (source ``scanner``, #128).
    scan_cover = models.ForeignKey(
        "ScanCover",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="library_covers",
    )
    # Provenance of the cover this one replaced, if any (Sprint020 G).
    replaced = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["book", "position"],
                name="uniq_cover_position_per_book",
            )
        ]

    def __str__(self) -> str:
        return f"{self.book_id}:{self.position}"


class CoverThumbnail(models.Model):
    """Small WebP rendition of a `CoverImage`: a derived, regenerable artifact (#28).

    Generated by a background job when a cover is stored and by the
    `generate_cover_thumbnails` backfill command. The file lives in a cache
    `StorageLocation` and is addressed by its own SHA-256, so its URL never
    changes meaning and can be cached as immutable. `source_sha256` records the
    cover bytes it was made from; a cover whose hash no longer matches gets a
    new thumbnail. The original cover is never modified.
    """

    class Size(models.TextChoices):
        SMALL = "small", "Small (list rows)"
        MEDIUM = "medium", "Medium (shelves and grids)"

    cover = models.ForeignKey(CoverImage, on_delete=models.CASCADE, related_name="thumbnails")
    size = models.CharField(max_length=16, choices=Size.choices)
    storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.PROTECT,
        related_name="cover_thumbnails",
    )
    storage_identifier = models.CharField(max_length=512)
    sha256 = models.CharField(max_length=64)
    source_sha256 = models.CharField(max_length=64)
    width = models.PositiveSmallIntegerField()
    height = models.PositiveSmallIntegerField()
    mime_type = models.CharField(max_length=32, default="image/webp")
    size_bytes = models.PositiveIntegerField()
    tool = models.CharField(max_length=64)
    tool_version = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["cover", "size"], name="uniq_cover_thumbnail_size"),
        ]
        indexes = [models.Index(fields=["sha256"], name="coverthumbnail_sha256_idx")]

    def __str__(self) -> str:
        return f"{self.cover_id}:{self.size}"


class CoverThumbnailFile(models.Model):
    """Registry row of one content-addressed thumbnail file (#28).

    Identical covers share a file, so a file may be referenced by several
    `CoverThumbnail` rows, and by none once they are replaced or deleted. Files
    are never deleted inline: `unreferenced_since` marks a file nothing points
    at, and `thumbnails.collect_unreferenced_files` deletes it after a grace
    period. Publication and collection both lock this row, so a file cannot be
    deleted while a worker is attaching a new reference to it. Files are only
    written under this lock, through a temp file and an atomic rename.
    """

    storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.PROTECT,
        related_name="cover_thumbnail_files",
    )
    storage_identifier = models.CharField(max_length=512)
    # The expected content: a file is current only when its size and SHA-256 match.
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField(default=0)
    unreferenced_since = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["storage_location", "storage_identifier"],
                name="uniq_cover_thumbnail_file",
            ),
        ]
        indexes = [
            models.Index(fields=["unreferenced_since"], name="coverthumbfile_unref_idx"),
        ]

    def __str__(self) -> str:
        return self.storage_identifier


class FileVariant(BookLinkedRecord):
    """One concrete representation of a book."""

    class VariantFormat(models.TextChoices):
        EPUB = "epub", "EPUB"
        MOBI = "mobi", "MOBI"
        AZW = "azw", "AZW"
        AZW3 = "azw3", "AZW3"
        PRC = "prc", "PRC"
        RTF = "rtf", "RTF"
        PDF = "pdf", "PDF"
        TXT = "txt", "TXT"
        HTML = "html", "HTML"
        FB2 = "fb2", "FB2"
        DJVU = "djvu", "DjVu"
        CBZ = "cbz", "CBZ"
        CBR = "cbr", "CBR"
        PNG = "png", "PNG"
        TIFF = "tiff", "TIFF"

    class SourceType(models.TextChoices):
        UPLOAD = "upload", "Upload"
        INCOMING_FOLDER = "incoming_folder", "Incoming folder"
        NETWORK_SOURCE = "network_source", "Network source"
        SCAN = "scan", "Scan"
        CONVERTED = "converted", "Converted"
        OCR = "ocr", "OCR"
        CLEANED = "cleaned", "Cleaned"
        GENERATED = "generated", "Generated"

    class ProcessingState(models.TextChoices):
        DISCOVERED = "discovered", "Discovered"
        HASHING = "hashing", "Hashing"
        QUEUED = "queued", "Queued"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="file_variants")
    format = models.CharField(max_length=16, choices=VariantFormat.choices)
    mime_type = models.CharField(max_length=128)
    original_filename = models.CharField(max_length=512)
    storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.PROTECT,
        related_name="file_variants",
    )
    storage_identifier = models.CharField(max_length=512)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_variants",
    )
    imported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    source_variant = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="derived_variants",
    )
    converter_tool = models.CharField(max_length=128, blank=True)
    converter_version = models.CharField(max_length=64, blank=True)
    converter_options = models.JSONField(default=dict, blank=True)
    processing_state = models.CharField(
        max_length=16,
        choices=ProcessingState.choices,
        default=ProcessingState.DISCOVERED,
    )
    job_id = models.UUIDField(default=None, null=True, blank=True)
    # Set only when this file is one part of a work split across several files (#60),
    # e.g. "ch07.pdf" of a book whose chapters are separate files. Null means the
    # variant is a complete representation of the book. The order and label were
    # confirmed by a reviewer; parts are never merged into or replaced by each other.
    part_index = models.PositiveIntegerField(null=True, blank=True)
    part_label = models.CharField(max_length=128, blank=True)

    class ReferenceState(models.TextChoices):
        OK = "ok", "OK"
        MISSING = "missing", "Missing from storage"
        CHANGED = "changed", "Changed on storage"

    # Drift tracking for variants referenced in place on a read-only location. Archived
    # variants keep the defaults; the storage verification command covers those.
    source_mtime_ns = models.BigIntegerField(null=True, blank=True)
    reference_state = models.CharField(
        max_length=16,
        choices=ReferenceState.choices,
        default=ReferenceState.OK,
    )
    reference_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["book", "format"]),
            models.Index(fields=["sha256"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["storage_location", "storage_identifier"],
                name="uniq_variant_storage_id",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.book_id}:{self.format}:{self.original_filename}"


class ScanJob(BookLinkedRecord):
    """Persistent scan job for physical book capture."""

    class State(models.TextChoices):
        OPEN = "open", "Open"
        CAPTURING = "capturing", "Capturing"
        PAUSED = "paused", "Paused"
        # A raw scan (#127, contract §5/D6): capture verified by checksum, awaiting
        # ScanTailor-Web processing and an explicit finish. Never in the library.
        CAPTURED = "captured", "Raw scan"
        FINALIZING = "finalizing", "Finalizing"
        COMPLETE = "complete", "Complete"
        CANCELLED = "cancelled", "Cancelled"

    class CaptureLayout(models.TextChoices):
        """What one uploaded image contains; decides how Page Split treats it (#55)."""

        SINGLE_PAGE = "single_page", "One page per image"
        SPREAD = "spread", "Two facing pages per image"

    class Source(models.TextChoices):
        """Who created the job: the scanner over ``/api/scanner/v1/`` or the web UI."""

        SCANNER = "scanner", "Scanner API"
        WEB = "web", "Web"

    # An opaque, immutable string (#126, libreflip contract §4.1/D1): the scanner-assigned
    # ID (YYYYMMDDTHHMMSSZ[-N]) for jobs registered over /api/scanner/v1/, a canonical
    # UUID string for jobs created in the web UI. See core.scan_job_ids.
    id = models.CharField(
        primary_key=True,
        max_length=MAX_SCAN_JOB_ID_LENGTH,
        default=new_scan_job_id,
        editable=False,
    )
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN)
    scanner_id = models.CharField(max_length=64, blank=True)
    # ``scanner`` for jobs registered over /api/scanner/v1/ (#126); only those are
    # visible to the scanner API. Everything else (web UI, session API) is ``web``.
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.WEB)
    # When the scanner started the job (§6.3); part of the registration identity.
    scan_started_at = models.DateTimeField(null=True, blank=True)
    capture_layout = models.CharField(
        max_length=16,
        choices=CaptureLayout.choices,
        default=CaptureLayout.SINGLE_PAGE,
    )
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_jobs",
    )
    book = models.ForeignKey(
        Book,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_jobs",
    )
    provisional_title = models.CharField(max_length=255, blank=True)
    provisional_authors = models.JSONField(default=list, blank=True)
    expected_page_count = models.PositiveIntegerField(null=True, blank=True)
    notes = models.JSONField(default=dict, blank=True)
    # Fencing token for provider searches: only the search job named here may
    # publish candidates for this source. Moved under this row's lock whenever a
    # new search supersedes an older one, so a stale queue entry or a late worker
    # can never make its results current (#104).
    current_search_job_id = models.UUIDField(null=True, blank=True, editable=False)
    # Projected from the scanner's scan.jsonl events (#129, contract §4.4): the
    # latest ``job`` event sets the calibration, the latest ``completeness_check`` the
    # flags. ``completeness_flags`` is NULL until a completeness check has arrived; an
    # empty list means the scanner found no gaps.
    page_width_mm = models.FloatField(null=True, blank=True)
    page_number_crop = models.JSONField(null=True, blank=True)
    completeness_flags = models.JSONField(null=True, blank=True)
    completeness_checked_at = models.DateTimeField(null=True, blank=True)
    # Highest ScanEvent id rendered into scan.jsonl in the job folder and in the book
    # folder, recorded only after the written file read back correctly (#129). Renders
    # decide whether a file is current by reading it, not from these marks.
    scan_jsonl_job_rendered_through = models.BigIntegerField(null=True, blank=True, editable=False)
    scan_jsonl_book_rendered_through = models.BigIntegerField(null=True, blank=True, editable=False)
    # Fences scan.jsonl renders: each pass takes the next generation under the row lock
    # and may only publish while its generation is still the newest (#129).
    scan_jsonl_render_generation = models.BigIntegerField(default=0, editable=False)
    # This job's scan.jsonl in its scan folder and in the book folder, each claimed with
    # an exclusive create at its first write ("scan.jsonl", else "scan-<job id>.jsonl",
    # else that name with a random token). serif only ever rewrites a file it claimed.
    scan_jsonl_job_identifier = models.CharField(max_length=1024, blank=True, editable=False)
    scan_jsonl_book_identifier = models.CharField(max_length=1024, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # capture-complete (§6.9): when serif verified the manifest, and the scanner's own
    # Finish Book time from that request.
    captured_at = models.DateTimeField(null=True, blank=True)
    capture_finished_at = models.DateTimeField(null=True, blank=True)
    # SHA-256 of the manifest capture-complete verified (items and event high-water
    # mark): an identical retry answers 200 again, any other manifest is re-checked.
    capture_manifest_sha256 = models.CharField(max_length=64, blank=True, default="")
    # A replacement page arrived after the job was complete (§5): the job is back in
    # `captured` and its existing library variants stay until the next finish.
    refinish_needed = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["state", "created_at"]),
            models.Index(fields=["scanner_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.id}:{self.state}"

    def is_active(self) -> bool:
        return self.state not in {self.State.COMPLETE, self.State.CANCELLED}


class ScanLog(models.Model):
    """A scanner log file uploaded for a scan job (#7), stored as ``scan-upload-<sha8>.log``.

    The upload request only stages the bytes and records the log as ``queued``; a
    worker (``ProcessingJob`` of type ``scan_log``) validates, hashes and stores it.
    Stored in the book's archive folder next to its files once the job is linked to a
    Book; until then it waits in scan storage and is moved there on finalization.
    """

    class State(models.TextChoices):
        QUEUED = "queued", "Queued"
        STORED = "stored", "Stored"
        DUPLICATE = "duplicate", "Duplicate of a stored log"
        FAILED = "failed", "Failed"

    scan_job = models.ForeignKey(ScanJob, on_delete=models.CASCADE, related_name="logs")
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    # Staged upload in scan storage while queued; cleared once processed.
    staging_identifier = models.CharField(max_length=512, blank=True, default="")
    storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.PROTECT,
        related_name="scan_logs",
        null=True,
        blank=True,
    )
    storage_identifier = models.CharField(max_length=512, blank=True, default="")
    original_filename = models.CharField(max_length=255)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64, blank=True, default="")
    duplicate_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duplicates",
    )
    processing_job = models.ForeignKey(
        "ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_logs",
    )
    error = models.CharField(max_length=500, blank=True, default="")
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_logs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # The same log content is stored once per job; re-uploads become DUPLICATE.
            models.UniqueConstraint(
                fields=["scan_job", "sha256"],
                condition=models.Q(state="stored"),
                name="uniq_scan_log_content",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.scan_job_id}:{self.original_filename}"


class ScanUploadSource(models.TextChoices):
    """Where a scan page or cover version came from (#128)."""

    WEB = "web", "Web upload"
    SCANNER = "scanner", "Scanner device API"


class ScanPage(models.Model):
    """One logical page within a scan job; multiple immutable versions are allowed."""

    class Side(models.TextChoices):
        SINGLE = "single", "Single"
        LEFT = "left", "Left"
        RIGHT = "right", "Right"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scan_job = models.ForeignKey(ScanJob, on_delete=models.CASCADE, related_name="pages")
    logical_sequence_number = models.PositiveIntegerField()
    side = models.CharField(max_length=16, choices=Side.choices, default=Side.SINGLE)
    # The printed page number the scanner recognised (a string, e.g. "23" or "xiv"),
    # and the scanner's capture status for the page ("ok", ...). Set with the device
    # page upload (#128) and from the latest ``page`` event in scan.jsonl (#129).
    recognized_page_number = models.CharField(max_length=64, null=True, blank=True)
    status = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scan_job", "logical_sequence_number", "side"],
                name="uniq_scanpage_sequence_side_per_job",
            ),
        ]
        indexes = [
            models.Index(fields=["scan_job", "logical_sequence_number", "side"]),
        ]

    def __str__(self) -> str:
        return f"job={self.scan_job_id}:seq={self.logical_sequence_number}:side={self.side}"

    def active_version(self) -> ScanPageVersion | None:
        return self.versions.filter(is_active=True).order_by("-version").first()


class ScanPageVersion(models.Model):
    """Immutable raw-page artifact versions for a logical scan page."""

    scan_page = models.ForeignKey(ScanPage, on_delete=models.CASCADE, related_name="versions")
    storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.PROTECT,
        related_name="scan_page_versions",
    )
    storage_identifier = models.CharField(max_length=512)
    checksum_sha256 = models.CharField(max_length=64)
    mime_type = models.CharField(max_length=128)
    size_bytes = models.PositiveBigIntegerField()
    capture_timestamp = models.DateTimeField(null=True, blank=True)
    scanner_metadata = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scan_page_versions",
    )
    # Who sent the version: a member in the web UI (``uploader``) or the scanner over
    # /api/scanner/v1/ (``uploader`` is then NULL, contract §9).
    source = models.CharField(
        max_length=16,
        choices=ScanUploadSource.choices,
        default=ScanUploadSource.WEB,
    )
    original_filename = models.CharField(max_length=512, blank=True)
    # A scanner rescan replacement's check of the printed page number (§6.11, #131):
    # False means the scanner's OCR did not match and the operator kept the image
    # anyway; shown next to the page in the workspace. NULL when nothing was said.
    page_number_verified = models.BooleanField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["scan_page", "version"]),
            models.Index(fields=["checksum_sha256"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["scan_page", "version"],
                name="uniq_scan_page_version_number",
            ),
            models.UniqueConstraint(
                fields=["scan_page"],
                condition=models.Q(is_active=True),
                name="uniq_scanpage_active_version",
            ),
        ]

    @property
    def scan_job(self) -> ScanJob:
        return self.scan_page.scan_job

    def __str__(self) -> str:
        return f"{self.scan_page_id}:v{self.version}:{self.checksum_sha256[:8]}"


class ScanCover(models.Model):
    """Immutable versions of a scan job's raw front/back cover photo (#128, §4.3, §6.6).

    The scanner uploads the raw photo; a rectified one, if it ever sends one, is a new
    version of the same position. Exactly one version per position is active. At finish
    the active versions become the Book's covers (``CoverImage.Source.SCANNER``).
    """

    class Position(models.TextChoices):
        FRONT = "front", "Front"
        BACK = "back", "Back"

    scan_job = models.ForeignKey(ScanJob, on_delete=models.CASCADE, related_name="covers")
    position = models.CharField(max_length=8, choices=Position.choices)
    version = models.PositiveIntegerField(default=1)
    storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.PROTECT,
        related_name="scan_covers",
    )
    storage_identifier = models.CharField(max_length=512)
    checksum_sha256 = models.CharField(max_length=64)
    mime_type = models.CharField(max_length=128)
    size_bytes = models.PositiveBigIntegerField()
    capture_timestamp = models.DateTimeField(null=True, blank=True)
    original_filename = models.CharField(max_length=512, blank=True)
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    source = models.CharField(
        max_length=16,
        choices=ScanUploadSource.choices,
        default=ScanUploadSource.SCANNER,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scan_job", "position", "version"],
                name="uniq_scancover_version_number",
            ),
            models.UniqueConstraint(
                fields=["scan_job", "position"],
                condition=models.Q(is_active=True),
                name="uniq_scancover_active_version",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.scan_job_id}:{self.position}:v{self.version}"


class ScanTodo(models.Model):
    """Rescan or other scan-page-level actions requiring scanner participation."""

    class TodoType(models.TextChoices):
        RESCAN = "rescan", "Rescan"

    class State(models.TextChoices):
        OPEN = "open", "Open"
        CLAIMED = "claimed", "Claimed"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    scan_job = models.ForeignKey(ScanJob, on_delete=models.CASCADE, related_name="todos")
    scan_page = models.ForeignKey(
        ScanPage,
        on_delete=models.CASCADE,
        related_name="todos",
    )
    todo_type = models.CharField(max_length=16, choices=TodoType.choices, default=TodoType.RESCAN)
    reason = models.TextField(blank=True)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_scan_todos",
    )
    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="claimed_scan_todos",
    )
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["scan_job", "state"]),
            models.Index(fields=["state", "todo_type"]),
        ]

    def is_open(self) -> bool:
        return self.state == self.State.OPEN

    def claim(self, user: User) -> None:
        if self.state == self.State.OPEN:
            self.state = self.State.CLAIMED
            self.claimed_by = user
            self.save(update_fields=["state", "claimed_by", "updated_at"])

    def complete(self) -> None:
        if self.state in {self.State.OPEN, self.State.CLAIMED}:
            self.state = self.State.COMPLETED
            self.save(update_fields=["state", "updated_at"])

    def cancel(self) -> None:
        if self.state not in {self.State.COMPLETED, self.State.CANCELLED}:
            self.state = self.State.CANCELLED
            self.save(update_fields=["state", "updated_at"])

    def __str__(self) -> str:
        return f"{self.scan_job_id}:{self.scan_page_id}:{self.todo_type}:{self.state}"


class ScanJobOpf(models.Model):
    """One immutable upload of a scan job's ``metadata.opf`` (#130, contract §4.5/§6.8).

    Every upload with a new checksum is a new version; the newest is current. The raw
    bytes are kept as received (at most ``BOOK_SCANNER_SCANNER_MAX_OPF_BYTES``), next to
    the fields parsed from them. An OPF that carries the application's own
    ``ebook-server:generated`` marker is stored but never used as metadata.
    """

    scan_job = models.ForeignKey(ScanJob, on_delete=models.CASCADE, related_name="opf_versions")
    version = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    content = models.BinaryField()
    # title, authors, isbns (ISBN-13 forms), isbn (as given), language, publisher,
    # description, subjects: see core.scanner_opf.parse_opf.
    fields = models.JSONField(default=dict, blank=True)
    app_generated = models.BooleanField(default=False)
    # Who sent this version. Only the scanner uploads OPFs today (one scanner, no
    # device registry); kept as a plain value so other origins can be added.
    source = models.CharField(max_length=16, default="scanner")
    # What applying this version to the linked Book did (updated fields, conflicts), and
    # the result of the background provider check (core.scanner_opf).
    application = models.JSONField(default=dict, blank=True)
    provider_check = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("scan_job", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=["scan_job", "version"], name="uniq_scan_job_opf_version"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.scan_job_id}:opf:v{self.version}"


class BookMetadataProvenance(models.Model):
    """History of where a Book's bibliographic fields came from (#130).

    One row per change (``action=set``) or event (``provider_check``, ``conflict``,
    ``confirmed``). The newest ``set`` row of a field is its current provenance: a field
    last set by a ``reviewer`` is never overwritten by a later scanner OPF.
    """

    class Source(models.TextChoices):
        SCANNER_OPF = "scanner_opf", "Scanner OPF"
        REVIEWER = "reviewer", "Reviewer"
        PROVIDER_CHECK = "provider_check", "External provider check"

    class Action(models.TextChoices):
        SET = "set", "Set"
        CONFIRMED = "confirmed", "Confirmed"
        CONFLICT = "conflict", "Conflict"
        PROVIDER_CHECK = "provider_check", "Provider check"

    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="metadata_provenance")
    # title, authors, isbn, language, publisher, description; empty for record events.
    field = models.CharField(max_length=32, blank=True)
    source = models.CharField(max_length=32, choices=Source.choices)
    action = models.CharField(max_length=32, choices=Action.choices, default=Action.SET)
    value = models.JSONField(null=True, blank=True)
    previous_value = models.JSONField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    scan_job = models.ForeignKey(
        ScanJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="metadata_provenance",
    )
    opf = models.ForeignKey(
        ScanJobOpf,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provenance",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("book", "-created_at", "-id")
        indexes = [models.Index(fields=["book", "field", "action"])]

    def __str__(self) -> str:
        return f"{self.book_id}:{self.field or '*'}:{self.source}:{self.action}"


class ScanEvent(models.Model):
    """One line of a scan job's ``scan.jsonl`` process log (#129, contract §4.4/§6.7).

    Lines from the scanner (origin ``sans``) are numbered ``1, 2, 3...`` in the scanner's
    file order; ``(scan_job, line_no)`` is unique for them and is the idempotency key.
    serif's own lines (origin ``serif``: ``rescan_flag``, ``rescan_replace``) have no
    ``line_no``. ``raw`` is the line exactly as received or written, without newline;
    ``type`` is copied out of it for queries. Rows are append-only. Their primary key is
    the arrival order: every insert for a job happens under that job's row lock (see
    ``core.scan_events``), so ordering a job's events by ``id`` is the order they arrived.
    """

    class Origin(models.TextChoices):
        SANS = "sans", "Scanner"
        SERIF = "serif", "Server"

    scan_job = models.ForeignKey(ScanJob, on_delete=models.CASCADE, related_name="events")
    origin = models.CharField(max_length=8, choices=Origin.choices)
    line_no = models.PositiveIntegerField(null=True, blank=True)
    raw = models.TextField()
    type = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scan_job", "line_no"],
                condition=models.Q(origin="sans"),
                name="uniq_scanevent_sans_line_no_per_job",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(origin="sans", line_no__isnull=False, line_no__gte=1)
                    | models.Q(origin="serif", line_no__isnull=True)
                ),
                name="scanevent_line_no_matches_origin",
            ),
        ]
        indexes = [
            models.Index(fields=["scan_job", "type"]),
        ]

    def __str__(self) -> str:
        suffix = f"#{self.line_no}" if self.line_no is not None else ""
        return f"{self.scan_job_id}:{self.origin}{suffix}:{self.type}"


class ScanTailorStageState(models.Model):
    """Per-page stage state for ScanTailor-Web processing."""

    class Stage(models.TextChoices):
        PAGE_SPLIT = "page_split", "Page split"
        DESKEW = "deskew", "Deskew"
        SELECT_CONTENT = "select_content", "Select content"
        MARGINS = "margins", "Margins"
        DEWARPING = "dewarping", "Dewarping"
        OUTPUT = "output", "Output/binarization"

    class StageStatus(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        INVALIDATED = "invalidated", "Invalidated"

    scan_job = models.ForeignKey(ScanJob, on_delete=models.CASCADE, related_name="stage_states")
    scan_page = models.ForeignKey(
        ScanPage,
        on_delete=models.CASCADE,
        related_name="stage_states",
    )
    stage = models.CharField(max_length=24, choices=Stage.choices)
    revision = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=16,
        choices=StageStatus.choices,
        default=StageStatus.UNKNOWN,
    )
    algorithm = models.CharField(max_length=128, blank=True)
    algorithm_version = models.CharField(max_length=64, blank=True)
    parameters = models.JSONField(default=dict, blank=True)
    input_stage_state = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="downstream_states",
        null=True,
        blank=True,
    )
    input_storage_identifier = models.CharField(max_length=512, blank=True)
    input_scan_page_version = models.ForeignKey(
        "ScanPageVersion",
        on_delete=models.SET_NULL,
        related_name="consumed_by_stage_states",
        null=True,
        blank=True,
    )
    output_storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.PROTECT,
        related_name="scan_stage_outputs",
        null=True,
        blank=True,
    )
    output_storage_identifier = models.CharField(max_length=512, blank=True)
    output_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    output_sha256 = models.CharField(max_length=64, blank=True)
    preview_storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.PROTECT,
        related_name="scan_stage_previews",
        null=True,
        blank=True,
    )
    preview_storage_identifier = models.CharField(max_length=512, blank=True)
    worker_job_id = models.CharField(max_length=64, blank=True)
    # Only genuine processing failures (status FAILED) go in last_error. Routine
    # bookkeeping such as "upstream stage changed" goes in invalidated_reason.
    last_error = models.TextField(blank=True)
    invalidated_reason = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scan_job", "scan_page", "stage"],
                name="uniq_scan_tailor_stage_per_page",
            ),
        ]
        indexes = [
            models.Index(fields=["scan_job", "scan_page", "stage"]),
            models.Index(fields=["status"]),
            models.Index(fields=["scan_page", "revision"]),
        ]

    def __str__(self) -> str:
        return f"{self.scan_page_id}:{self.stage}:rev={self.revision}:{self.status}"


class ProcessingJob(models.Model):
    """Base processing-job state for conversion/OCR/ScanTailor stages."""

    class JobType(models.TextChoices):
        INGESTION = "ingestion", "Ingestion"
        CONVERSION = "conversion", "Conversion"
        OCR = "ocr", "OCR"
        STAGE = "stage", "ScanTailor Stage"
        REVIEW_IDENTIFICATION = "review_identification", "Review identification"
        SCAN_IDENTIFICATION = "scan_identification", "Scan identification"
        SCAN_FINALIZATION = "scan_finalization", "Scan finalization"
        LLM_IDENTIFICATION = "llm_identification", "LLM identification assist"
        COMBINE_PARTS = "combine_parts", "Combine multi-part work"
        REFERENCE_CHECK = "reference_check", "Referenced file check"
        SCANNER_OPF_CHECK = "scanner_opf_check", "Scanner OPF provider check"
        SCAN_LOG = "scan_log", "Scanner log"
        ARCHIVE_REORGANIZE = "archive_reorganize", "Archive reorganize"

    class JobState(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    job_id = models.UUIDField(unique=True, default=uuid.uuid4)
    file_variant = models.ForeignKey(
        FileVariant,
        on_delete=models.CASCADE,
        related_name="processing_jobs",
        null=True,
        blank=True,
    )
    job_type = models.CharField(max_length=32, choices=JobType.choices)
    state = models.CharField(max_length=16, choices=JobState.choices, default=JobState.QUEUED)
    details = models.JSONField(default=dict, blank=True)
    scheduled_at = models.DateTimeField(default=now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # At most one queued/running combine job per book and target format (#60):
            # concurrent first requests cannot both enqueue one.
            models.UniqueConstraint(
                KT("details__book_id"),
                KT("details__target_format"),
                condition=models.Q(job_type="combine_parts", state__in=["queued", "running"]),
                name="uniq_active_combine_job_per_book_format",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.job_id}:{self.job_type}"


class ImportGroup(models.Model):
    """Source files that intake suspects belong together, pending a reviewer's decision.

    Today the only kind is a work split across several per-chapter files in one
    folder (#60). Detection is a filename heuristic, so the group stays PENDING until
    a reviewer confirms it (parts of one Book) or rejects it (independent imports).
    """

    class Kind(models.TextChoices):
        MULTIPART_WORK = "multipart_work", "Multi-part work"

    class State(models.TextChoices):
        PENDING = "pending", "Pending review"
        CONFIRMED = "confirmed", "Confirmed as one work"
        REJECTED = "rejected", "Rejected (independent imports)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=32, choices=Kind.choices, default=Kind.MULTIPART_WORK)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    source_location = models.ForeignKey(
        "SourceLocation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_groups",
    )
    folder = models.CharField(max_length=1024)
    group_key = models.CharField(max_length=512)
    detection = models.JSONField(default=dict, blank=True)
    book = models.ForeignKey(
        Book,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_groups",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["source_location", "state"])]
        constraints = [
            models.UniqueConstraint(
                fields=["source_location", "folder", "group_key"],
                condition=models.Q(state="pending"),
                name="uniq_pending_import_group_per_folder_key",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.id}:{self.kind}:{self.state}"


class ImportJob(models.Model):
    """Stateful intake job for uploaded or discovered source files."""

    class State(models.TextChoices):
        DISCOVERED = "discovered", "Discovered"
        HASHING = "hashing", "Hashing"
        METADATA_EXTRACTION = "metadata_extraction", "Metadata extraction"
        SIGNAL_NORMALIZATION = "signal_normalization", "Signal normalization"
        EXTERNAL_IDENTIFICATION = "external_identification", "External identification"
        MATCHED = "matched", "Matched"
        LOW_CONFIDENCE = "low_confidence", "Low confidence"
        ARCHIVING = "archiving", "Archiving"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    class SourceType(models.TextChoices):
        UPLOAD = "upload", "Upload"
        INCOMING_FOLDER = "incoming_folder", "Incoming folder"
        NETWORK_SOURCE = "network_source", "Network source"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    state = models.CharField(max_length=32, choices=State.choices, default=State.DISCOVERED)
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="import_jobs",
    )
    source_location = models.ForeignKey(
        "SourceLocation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
    )
    source_path = models.CharField(
        max_length=1024,
        help_text="Source path for intake file to be processed.",
    )
    original_filename = models.CharField(max_length=512)
    normalized_filename = models.CharField(max_length=512, blank=True)
    detected_format = models.CharField(max_length=16, blank=True)
    mime_type = models.CharField(max_length=255, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    storage_identifier = models.CharField(max_length=512, blank=True)
    storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
    )
    book = models.ForeignKey(
        Book,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
    )
    file_variant = models.ForeignKey(
        "FileVariant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
    )
    attempt_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    # Fencing token for provider searches: only the search job named here may
    # publish candidates for this source. Moved under this row's lock whenever a
    # new search supersedes an older one, so a stale queue entry or a late worker
    # can never make its results current (#104).
    current_search_job_id = models.UUIDField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    source_size = models.PositiveBigIntegerField(null=True, blank=True)
    source_mtime_ns = models.BigIntegerField(null=True, blank=True)
    # Membership in a suspected multi-part work (#60) and this file's proposed place in it.
    import_group = models.ForeignKey(
        ImportGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
    )
    part_index = models.PositiveIntegerField(null=True, blank=True)
    part_label = models.CharField(max_length=128, blank=True)
    # Exclusive ownership while a worker processes the job (HASHING through ARCHIVING).
    # Set atomically when a delivery claims the job, renewed on every phase transition,
    # and cleared when the job leaves the active phases. Empty means unowned.
    lease_owner = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["state", "source_type"]),
            models.Index(fields=["sha256"]),
        ]

    def mark_retryable(self, error: str) -> None:
        self.state = self.State.FAILED
        self.last_error = error
        self.attempt_count += 1
        self.next_retry_at = datetime.now(UTC)
        self.save(
            update_fields=[
                "state",
                "last_error",
                "attempt_count",
                "next_retry_at",
                "updated_at",
            ],
        )

    def __str__(self) -> str:
        return f"{self.id}:{self.state}"


class MetadataSearch(models.Model):
    """One finished identification run against the configured metadata providers.

    Records the terms that were searched, what every provider did (results, no
    results, or an error and why) and, through ``candidates``, what it returned.
    Runs are kept, so a failed or empty repeat search never hides earlier results.
    A run is written only once it has finished; whether a search is in progress is
    derived from its queued/running ``ProcessingJob``, never from this table.
    """

    class Trigger(models.TextChoices):
        INTAKE = "intake", "Import"
        REVIEW = "review", "Review search"
        SCAN = "scan", "Scan identification"

    class State(models.TextChoices):
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"
        # Finished, but its results were kept as history only: a newer search
        # replaced it or the review was resolved while it ran. ``error`` says which.
        DISCARDED = "discarded", "Not applied"

    PROVIDER_STATUS_LABELS: ClassVar[dict[str, str]] = {
        "results": "Results",
        "no_results": "No results",
        "error": "Error",
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    import_job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="metadata_searches",
        null=True,
        blank=True,
    )
    scan_job = models.ForeignKey(
        ScanJob,
        on_delete=models.CASCADE,
        related_name="metadata_searches",
        null=True,
        blank=True,
    )
    trigger = models.CharField(max_length=16, choices=Trigger.choices)
    state = models.CharField(max_length=16, choices=State.choices)
    processing_job_id = models.UUIDField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="metadata_searches",
        null=True,
        blank=True,
    )
    # The terms actually sent to the providers (title/author hints, ISBNs, ...).
    query = models.JSONField(default=dict, blank=True)
    # One ProviderOutcome.as_dict() per configured provider.
    providers = models.JSONField(default=list, blank=True)
    candidate_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(default=now)
    finished_at = models.DateTimeField(default=now)

    class Meta:
        indexes = [
            models.Index(fields=["import_job", "-finished_at"]),
            models.Index(fields=["scan_job", "-finished_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(import_job__isnull=False, scan_job__isnull=True)
                    | models.Q(import_job__isnull=True, scan_job__isnull=False)
                ),
                name="search_has_exactly_one_source",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.trigger}:{self.id}:{self.state}"

    @property
    def provider_rows(self) -> list[dict[str, object]]:
        """Provider outcomes with a display label for their status."""
        return [
            {**row, "status_label": self.PROVIDER_STATUS_LABELS.get(str(row.get("status")), "")}
            for row in self.providers
            if isinstance(row, dict)
        ]

    @property
    def provider_errors(self) -> list[dict[str, object]]:
        return [row for row in self.provider_rows if row.get("status") == "error"]


class MetadataCandidate(models.Model):
    """Candidate bibliographic matches returned during identification.

    Candidates of earlier search runs are kept for history with ``superseded_at``
    set; only current candidates (``superseded_at`` is NULL) can be accepted and
    count towards metrics and review-queue payloads.
    """

    class CandidateState(models.TextChoices):
        MATCH = "match", "Match"
        REVIEW = "review", "Review"
        REJECTED = "rejected", "Rejected"

    import_job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="metadata_candidates",
        null=True,
        blank=True,
    )
    scan_job = models.ForeignKey(
        ScanJob,
        on_delete=models.CASCADE,
        related_name="metadata_candidates",
        null=True,
        blank=True,
    )
    search = models.ForeignKey(
        MetadataSearch,
        on_delete=models.SET_NULL,
        related_name="candidates",
        null=True,
        blank=True,
    )
    superseded_at = models.DateTimeField(null=True, blank=True)
    provider_id = models.CharField(max_length=64)
    provider_ref = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=512)
    authors = models.JSONField(default=list, blank=True)
    isbn10 = models.CharField(max_length=16, blank=True)
    isbn13 = models.CharField(max_length=16, blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    raw_payload = models.JSONField(default=dict, blank=True)
    # Provider-reported subject/genre/category tags. Raw evidence; tags that map to a
    # Genre (or GenreAlias) are attached to the Book on accept (#50).
    subjects = models.JSONField(default=list, blank=True)
    language = models.CharField(max_length=32, blank=True)
    publisher = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    publication_year = models.PositiveIntegerField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    state = models.CharField(
        max_length=16,
        choices=CandidateState.choices,
        default=CandidateState.REVIEW,
    )
    decision_notes = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["import_job", "score"]),
            models.Index(fields=["scan_job", "score"]),
            models.Index(fields=["provider_id", "state"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(import_job__isnull=False, scan_job__isnull=True)
                    | models.Q(import_job__isnull=True, scan_job__isnull=False)
                ),
                name="candidate_has_exactly_one_source",
            ),
            # Unique among current candidates only: superseded runs may hold the
            # same provider record again.
            models.UniqueConstraint(
                fields=["import_job", "provider_id", "provider_ref"],
                condition=models.Q(import_job__isnull=False, superseded_at__isnull=True),
                name="uniq_current_import_candidate_provider_ref",
            ),
            models.UniqueConstraint(
                fields=["scan_job", "provider_id", "provider_ref"],
                condition=models.Q(scan_job__isnull=False, superseded_at__isnull=True),
                name="uniq_current_scan_candidate_provider_ref",
            ),
        ]

    def __str__(self) -> str:
        source_id = self.import_job_id or self.scan_job_id
        return f"{source_id}:{self.provider_id}:{self.provider_ref}:{self.score}"


class ReviewTask(models.Model):
    """Reusable workflow task for manual human resolution."""

    class TaskType(models.TextChoices):
        NEEDS_ATTENTION = "needs_attention", "Needs attention"
        SOURCE_REVIEW = "source_review", "Source review"
        QUALITY_REVIEW = "quality_review", "Quality review"
        MULTIPART_WORK = "multipart_work", "Multi-part work"

    class State(models.TextChoices):
        OPEN = "open", "Open"
        CLAIMED = "claimed", "Claimed"
        RESOLVED = "resolved", "Resolved"
        CANCELLED = "cancelled", "Cancelled"

    class Decision(models.TextChoices):
        UNRESOLVED = "unresolved", "Unresolved"
        ACCEPT_CANDIDATE = "accept_candidate", "Accept candidate"
        SEARCH_AGAIN = "search_again", "Search again"
        EDIT_SEARCH_TERMS = "edit_search_terms", "Edit search terms"
        MANUAL_ENTRY = "manual_entry", "Manual entry"
        MERGED_BOOK = "merged_book", "Merged with existing book"
        MARK_UNIDENTIFIED = "mark_unidentified", "Mark unidentified"
        SPLIT_PARTS = "split_parts", "Not one work: import parts separately"
        # A scanner-OPF conflict (#130): keep or replace the Book's field values.
        METADATA_CONFLICT = "metadata_conflict", "Resolved a metadata conflict"

    import_job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="review_tasks",
        null=True,
        blank=True,
    )
    scan_job = models.ForeignKey(
        ScanJob,
        on_delete=models.CASCADE,
        related_name="review_tasks",
        null=True,
        blank=True,
    )
    task_type = models.CharField(
        max_length=32,
        choices=TaskType.choices,
        default=TaskType.NEEDS_ATTENTION,
    )
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_review_tasks",
    )
    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="claimed_review_tasks",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_review_tasks",
    )
    source_filename = models.CharField(max_length=512)
    source_path_hint = models.CharField(max_length=1024, blank=True)
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploader_review_tasks",
    )
    inferred_title = models.CharField(max_length=255, blank=True)
    inferred_authors = models.JSONField(default=list, blank=True)
    embedded_metadata = models.JSONField(default=dict, blank=True)
    provider_snapshot = models.JSONField(default=dict, blank=True)
    decision = models.CharField(
        max_length=32,
        choices=Decision.choices,
        default=Decision.UNRESOLVED,
    )
    decision_payload = models.JSONField(default=dict, blank=True)
    resolved_book = models.ForeignKey(
        "Book",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review_tasks",
    )
    selected_candidate = models.ForeignKey(
        MetadataCandidate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review_tasks",
    )
    decision_notes = models.TextField(blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["state", "task_type"]),
            models.Index(fields=["created_by", "state"]),
            models.Index(fields=["resolved_book"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(import_job__isnull=False, scan_job__isnull=True)
                    | models.Q(import_job__isnull=True, scan_job__isnull=False)
                ),
                name="review_has_exactly_one_source",
            ),
        ]

    def is_resolved(self) -> bool:
        return self.state == self.State.RESOLVED

    def can_be_resolved_by(self, user: User) -> bool:
        try:
            role = user.library_profile.role
        except Exception:
            return False
        if role == UserRole.ADMIN:
            return True
        import_job = self.import_job
        if import_job is not None:
            return user == import_job.uploader
        scan_job = self.scan_job
        if scan_job is not None:
            return user == scan_job.operator
        return False

    def __str__(self) -> str:
        source_id = self.import_job_id or self.scan_job_id
        return f"{source_id}:{self.task_type}:{self.state}"


class LLMSuggestion(models.Model):
    """Validated, structured output of one local-LLM identification request (#26).

    Evidence only, never truth: the hints were used as provider search terms, and any
    candidate they found still needs a reviewer. The evidence bundle itself (which may
    contain book text) is not stored, only its SHA-256.
    """

    class Trigger(models.TextChoices):
        AUTO_ZERO_CANDIDATES = "auto_zero_candidates", "Automatic: no candidates"
        AUTO_LOW_SCORE = "auto_low_score", "Automatic: low score"
        REVIEWER = "reviewer", "Reviewer request"

    class Outcome(models.TextChoices):
        HINTS_USED = "hints_used", "Hints found new candidates"
        NO_NEW_CANDIDATES = "no_new_candidates", "No new candidates"
        INVALID_OUTPUT = "invalid_output", "Invalid model output"
        FAILED = "failed", "Failed"

    import_job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="llm_suggestions",
        null=True,
        blank=True,
    )
    scan_job = models.ForeignKey(
        ScanJob,
        on_delete=models.CASCADE,
        related_name="llm_suggestions",
        null=True,
        blank=True,
    )
    review_task = models.ForeignKey(
        ReviewTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="llm_suggestions",
    )
    processing_job = models.ForeignKey(
        ProcessingJob,
        on_delete=models.CASCADE,
        related_name="llm_suggestions",
    )
    trigger = models.CharField(max_length=32, choices=Trigger.choices)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="llm_suggestions",
    )
    model = models.CharField(max_length=255, blank=True)
    model_version = models.CharField(max_length=255, blank=True)
    prompt_template_version = models.CharField(max_length=32, blank=True)
    input_sha256 = models.CharField(max_length=64, blank=True)
    output = models.JSONField(default=dict, blank=True)
    grounding = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=32, choices=Outcome.choices)
    new_candidate_count = models.PositiveIntegerField(default=0)
    duration_seconds = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(import_job__isnull=False, scan_job__isnull=True)
                    | models.Q(import_job__isnull=True, scan_job__isnull=False)
                ),
                name="llm_suggestion_has_exactly_one_source",
            ),
        ]

    def __str__(self) -> str:
        source_id = self.import_job_id or self.scan_job_id
        return f"{source_id}:llm:{self.outcome}"


def canonical_ldap_uid(uid: str) -> str:
    """Canonical form for a directory uid typed by a user (directories are case-insensitive)."""
    return uid.strip().casefold()


def get_or_create_profile(
    ldap_uid: str,
    *,
    role: str | UserRole = DEFAULT_USER_ROLE,
    is_disabled: bool = False,
) -> AppUserProfile:
    """Create or fetch profile record for an authenticated LDAP identity.

    Lookups are case-insensitive so "Alice" and "alice" resolve to the same account.
    New accounts are created with the identity exactly as given; callers should pass a
    canonical uid (the directory's uid attribute, or the casefolded login name).
    """

    ldap_uid = ldap_uid.strip()
    user_model = get_user_model()
    profile = (
        AppUserProfile.objects.select_related("user")
        .filter(ldap_uid__iexact=ldap_uid)
        .order_by("pk")
        .first()
    )
    if profile is not None:
        return profile
    user = user_model.objects.filter(username__iexact=ldap_uid).order_by("pk").first()
    if user is None:
        user = user_model.objects.create(username=ldap_uid, is_active=True)
    normalized_role = role.value if isinstance(role, UserRole) else role
    profile, _created = AppUserProfile.objects.get_or_create(
        ldap_uid=ldap_uid,
        defaults={
            "user": user,
            "role": normalized_role,
            "is_disabled": is_disabled,
        },
    )
    return profile


# --- Contribution recognition (#27, docs/contributions.md) ------------------------------


class ContributionKind(models.TextChoices):
    REVIEW_RESOLVED = "review_resolved", "Review resolved"
    REVIEW_UNIDENTIFIED = "review_unidentified", "Review marked unidentified"
    SCAN_COMPLETED = "scan_completed", "Scan finished into the library"
    CLAWBACK = "clawback", "Clawback"
    ADMIN_ADJUSTMENT = "admin_adjustment", "Admin adjustment"


class ContributionEvent(models.Model):
    """Append-only ledger of contribution outcomes.

    Emitters create PENDING rows next to the state transitions they describe; they do
    not compute points. Only the settlement job (and the admin void action) moves a row
    from PENDING to SETTLED or VOIDED and fills ``points``. A reversal found after
    settlement never edits the row: it adds a negative CLAWBACK row that ``reverses`` it.
    """

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        SETTLED = "settled", "Settled"
        VOIDED = "voided", "Voided"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="contribution_events",
    )
    kind = models.CharField(max_length=32, choices=ContributionKind.choices)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    points = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    weight_version = models.PositiveIntegerField(default=0)
    own_upload = models.BooleanField(default=False)
    # Idempotency key of the outcome, e.g. "review:<task>:<decided_at>"; one event per
    # (user, kind, outcome) so retries and reprocessing never double-count.
    outcome_key = models.CharField(max_length=200)
    # Snapshot of the outcome when it happened; settlement compares the source rows
    # against it to find reversals.
    outcome = models.JSONField(default=dict, blank=True)
    review_task = models.ForeignKey(
        ReviewTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contribution_events",
    )
    scan_job = models.ForeignKey(
        ScanJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contribution_events",
    )
    book = models.ForeignKey(
        Book,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contribution_events",
    )
    reverses = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reversals",
    )
    reason = models.CharField(max_length=255, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    occurred_at = models.DateTimeField()
    settle_after = models.DateTimeField()
    settled_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind", "outcome_key"],
                name="uniq_contribution_event_outcome",
            ),
        ]
        indexes = [
            models.Index(fields=["state", "settle_after"]),
            models.Index(fields=["user", "state", "occurred_at"]),
            models.Index(fields=["kind", "state", "occurred_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.kind}:{self.state}:{self.points}"


class ContributionWeight(models.Model):
    """Admin-configurable points per outcome kind; every edit is a new version."""

    version = models.PositiveIntegerField()
    kind = models.CharField(max_length=32, choices=ContributionKind.choices)
    base_points = models.DecimalField(max_digits=7, decimal_places=2)
    per_unit_points = models.DecimalField(max_digits=7, decimal_places=3, default=0)
    cap = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    own_upload_multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["version", "kind"],
                name="uniq_contribution_weight_version_kind",
            ),
        ]

    def __str__(self) -> str:
        return f"v{self.version}:{self.kind}:{self.base_points}"


class ContributorPreference(models.Model):
    """Leaderboard consent and display handle; no row means "not on the board"."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="contributor_preference",
    )
    leaderboard_opt_in = models.BooleanField(default=False)
    display_handle = models.CharField(max_length=40, blank=True)
    opted_in_at = models.DateTimeField(null=True, blank=True)
    excluded_from_leaderboard = models.BooleanField(default=False)
    excluded_reason = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("display_handle"),
                condition=models.Q(leaderboard_opt_in=True) & ~models.Q(display_handle=""),
                name="uniq_contributor_handle_opted_in",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.display_handle}:{self.leaderboard_opt_in}"


class ReviewDecisionCorrection(models.Model):
    """A later finding that a resolved ReviewTask decision no longer stands."""

    class Source(models.TextChoices):
        REOPENED = "reopened", "Review reopened"
        RE_RESOLVED = "re_resolved", "Resolved again"
        BOOK_DELETED = "book_deleted", "Resolved book deleted"
        BOOK_MERGED = "book_merged", "Resolved book merged away"
        SPOT_CHECK = "spot_check", "Spot-check found it wrong"
        ADMIN = "admin", "Admin flagged it wrong"

    review_task = models.ForeignKey(
        ReviewTask,
        on_delete=models.CASCADE,
        related_name="decision_corrections",
    )
    found_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    source = models.CharField(max_length=16, choices=Source.choices)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["review_task", "created_at"])]

    def __str__(self) -> str:
        return f"{self.review_task_id}:{self.source}"


class ContributionMonthlyTotal(models.Model):
    """Monthly aggregate kept after retention pruning removes the detailed events."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="contribution_monthly_totals",
    )
    month = models.DateField()
    kind = models.CharField(max_length=32, choices=ContributionKind.choices)
    points = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    event_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "month", "kind"],
                name="uniq_contribution_monthly_total",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.month}:{self.kind}:{self.points}"
