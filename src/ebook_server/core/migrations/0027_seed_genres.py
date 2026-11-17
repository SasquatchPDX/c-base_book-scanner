"""Seed a small starter genre taxonomy with common tag spellings (#50).

Admins edit, extend or delete these in the Django admin. Matching folds case, accents,
``&``/``and``, punctuation and spacing (``models.genre_key``), so an alias only needs to
be listed once per distinct wording. BISAC strings ("Fiction / Science Fiction") and
Open Library style strings ("Fiction, science fiction") are listed explicitly; the
resolver also drops trailing "General" segments and walks up BISAC hierarchies.
"""

import re
import unicodedata

from django.db import migrations

SEED_GENRES: dict[str, list[str]] = {
    "Science fiction": [
        "Sci-Fi",
        "SF",
        "Science-fiction",
        "Science fiction stories",
        "Fiction / Science Fiction",
        "Fiction, science fiction",
        "Science fiction, american",
        "Science fiction, english",
    ],
    "Fantasy": [
        "Fantasy fiction",
        "Fiction / Fantasy",
        "Fiction, fantasy",
    ],
    "Horror": [
        "Horror fiction",
        "Horror tales",
        "Fiction / Horror",
        "Fiction, horror",
    ],
    "Mystery": [
        "Mystery fiction",
        "Mystery & detective",
        "Detective and mystery stories",
        "Fiction / Mystery & Detective",
        "Fiction, mystery & detective",
    ],
    "Crime": [
        "Crime fiction",
        "Fiction / Crime",
    ],
    "Thriller": [
        "Thrillers",
        "Suspense",
        "Thrillers & suspense",
        "Suspense fiction",
        "Fiction / Thrillers",
        "Fiction, thrillers",
        "Fiction, suspense",
    ],
    "Romance": [
        "Love stories",
        "Romance fiction",
        "Fiction / Romance",
        "Fiction, romance",
    ],
    "Historical fiction": [
        "Fiction / Historical",
        "Fiction, historical",
    ],
    "Literary fiction": [
        "Fiction / Literary",
    ],
    "Adventure": [
        "Adventure stories",
        "Adventure fiction",
        "Action & adventure",
        "Fiction / Action & Adventure",
        "Fiction, action & adventure",
    ],
    "Dystopian": [
        "Dystopias",
        "Dystopian fiction",
        "Fiction / Dystopian",
    ],
    "Humor": [
        "Humour",
        "Humorous fiction",
        "Humorous stories",
        "Fiction / Humorous",
    ],
    "Short stories": [
        "Fiction / Short Stories (single author)",
        "Fiction / Anthologies (multiple authors)",
    ],
    "Comics & graphic novels": [
        "Comics",
        "Comic books, strips, etc.",
        "Graphic novels",
    ],
    "Poetry": [],
    "Drama": [
        "Plays",
    ],
    "Biography & memoir": [
        "Biography",
        "Biographies",
        "Autobiography",
        "Memoir",
        "Memoirs",
        "Biography & Autobiography",
    ],
    "History": [],
    "Science": [
        "Popular science",
    ],
    "Computers & technology": [
        "Computers",
        "Computer science",
        "Computer programming",
        "Technology",
        "Technology & Engineering",
    ],
    "Philosophy": [],
    "Self-help": [],
}


def _genre_key(value):
    # Frozen copy of ``core.models.genre_key`` at the time of this migration.
    text = unicodedata.normalize("NFKD", value).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("&", " and ")
    return re.sub(r"[\W_]+", "", text)


def seed_genres(apps, schema_editor):
    Genre = apps.get_model("core", "Genre")
    GenreAlias = apps.get_model("core", "GenreAlias")
    seeded = {}
    for name in SEED_GENRES:
        name_keys = {
            _genre_key(other): other for other in Genre.objects.values_list("name", flat=True)
        }
        existing = name_keys.get(_genre_key(name))
        # An admin may already have an equivalent genre; reuse it instead of a twin. Only
        # rows created here are marked ``seeded`` (owned by this migration).
        seeded[name] = (
            Genre.objects.get(name=existing)
            if existing
            else Genre.objects.create(name=name, seeded=True)
        )
    name_keys = {_genre_key(name) for name in Genre.objects.values_list("name", flat=True)}
    for name, aliases in SEED_GENRES.items():
        for alias in aliases:
            key = _genre_key(alias)
            if key in name_keys or GenreAlias.objects.filter(key=key).exists():
                continue
            GenreAlias.objects.create(genre=seeded[name], alias=alias, key=key, seeded=True)


def unseed_genres(apps, schema_editor):
    """Remove only what ``seed_genres`` created, and only genres nobody has used.

    Rows that existed before the migration (``seeded=False``) are never touched, even
    when their names or aliases match the seed list. A seeded genre that has books or
    admin-added aliases is kept.
    """
    Genre = apps.get_model("core", "Genre")
    GenreAlias = apps.get_model("core", "GenreAlias")
    GenreAlias.objects.filter(seeded=True).delete()
    Genre.objects.filter(seeded=True, books__isnull=True, aliases__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0026_genre_alias_book_genre_provenance"),
    ]

    operations = [
        migrations.RunPython(seed_genres, unseed_genres),
    ]
