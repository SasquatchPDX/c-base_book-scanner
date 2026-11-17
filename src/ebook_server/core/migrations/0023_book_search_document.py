"""Denormalised library search rows with PostgreSQL FTS and trigram indexes (#28).

The GIN indexes and the pg_trgm extension exist only on PostgreSQL. On other
databases (SQLite in development and the unit tests) the operations update the
migration state but run no SQL, and the search service falls back to plain
`icontains` filters.
"""

import django.contrib.postgres.indexes
import django.contrib.postgres.search
import django.db.models.deletion
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models


class AddIndexOnPostgreSQL(migrations.AddIndex):
    """`AddIndex` that only touches the database on PostgreSQL (GIN indexes)."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_backwards(app_label, schema_editor, from_state, to_state)


class TrigramExtensionOnPostgreSQL(TrigramExtension):
    """`CreateExtension` checks the vendor going forwards but not backwards."""

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_backwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0022_source_location_group_sibling_formats"),
    ]

    operations = [
        TrigramExtensionOnPostgreSQL(),
        migrations.CreateModel(
            name="BookSearchDocument",
            fields=[
                (
                    "book",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="search_document",
                        serialize=False,
                        to="core.book",
                    ),
                ),
                (
                    "document",
                    django.contrib.postgres.search.SearchVectorField(blank=True, null=True),
                ),
                ("title_sort", models.CharField(blank=True, max_length=255)),
                ("author_sort", models.CharField(blank=True, max_length=255)),
                ("author_text", models.TextField(blank=True)),
                ("language", models.CharField(blank=True, max_length=32)),
                ("publication_decade", models.SmallIntegerField(blank=True, null=True)),
                ("has_cover", models.BooleanField(default=False)),
                ("book_created_at", models.DateTimeField()),
                ("indexed_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["-book_created_at", "-book"],
                        name="booksearch_created_idx",
                    ),
                    models.Index(fields=["title_sort", "book"], name="booksearch_title_idx"),
                    models.Index(fields=["author_sort", "book"], name="booksearch_author_idx"),
                    models.Index(fields=["language", "book"], name="booksearch_language_idx"),
                ],
            },
        ),
        AddIndexOnPostgreSQL(
            model_name="booksearchdocument",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["document"], name="booksearch_document_gin"
            ),
        ),
        AddIndexOnPostgreSQL(
            model_name="booksearchdocument",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["title_sort"],
                name="booksearch_title_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        AddIndexOnPostgreSQL(
            model_name="booksearchdocument",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["author_text"],
                name="booksearch_author_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        migrations.CreateModel(
            name="BookSearchFacet",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("format", "Format"),
                            ("author", "Author"),
                            ("genre", "Genre"),
                            ("category", "Category"),
                        ],
                        max_length=16,
                    ),
                ),
                ("value", models.CharField(max_length=64)),
                (
                    "book",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="search_facets",
                        to="core.book",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["kind", "value", "book"],
                        name="booksearchfacet_lookup_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("book", "kind", "value"),
                        name="uniq_booksearchfacet_book_kind_value",
                    )
                ],
            },
        ),
    ]
