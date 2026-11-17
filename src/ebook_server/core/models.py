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
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils.timezone import now


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


class BookGenre(models.Model):
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


class Identifier(models.Model):
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


class CoverImage(models.Model):
    """Stored front/back cover image per book."""

    class CoverPosition(models.TextChoices):
        FRONT = "front", "Front"
        BACK = "back", "Back"

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


class FileVariant(models.Model):
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


class ScanJob(models.Model):
    """Persistent scan job for physical book capture."""

    class State(models.TextChoices):
        OPEN = "open", "Open"
        CAPTURING = "capturing", "Capturing"
        PAUSED = "paused", "Paused"
        FINALIZING = "finalizing", "Finalizing"
        COMPLETE = "complete", "Complete"
        CANCELLED = "cancelled", "Cancelled"

    class CaptureLayout(models.TextChoices):
        """What one uploaded image contains; decides how Page Split treats it (#55)."""

        SINGLE_PAGE = "single_page", "One page per image"
        SPREAD = "spread", "Two facing pages per image"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN)
    scanner_id = models.CharField(max_length=64, blank=True)
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["state", "created_at"]),
            models.Index(fields=["scanner_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.id}:{self.state}"

    def is_active(self) -> bool:
        return self.state not in {self.State.COMPLETE, self.State.CANCELLED}


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
    original_filename = models.CharField(max_length=512, blank=True)
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
        REFERENCE_CHECK = "reference_check", "Referenced file check"

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

    def __str__(self) -> str:
        return f"{self.job_id}:{self.job_type}"


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
