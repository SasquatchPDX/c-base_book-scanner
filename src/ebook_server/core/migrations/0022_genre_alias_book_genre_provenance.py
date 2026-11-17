import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    """Genre aliases and per-link provenance for Book.genres (#50).

    ``Book.genres`` keeps its existing ``core_book_genres`` table: the explicit
    ``BookGenre`` through model is first adopted in state only, then gains provenance
    columns. Existing links (hand-set in the admin) become ``source="admin"``.
    """

    dependencies = [
        ("core", "0021_source_location_path_layouts"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="BookGenre",
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
                            "book",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="core.book",
                            ),
                        ),
                        (
                            "genre",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="core.genre",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "core_book_genres",
                        "unique_together": {("book", "genre")},
                    },
                ),
                migrations.AlterField(
                    model_name="book",
                    name="genres",
                    field=models.ManyToManyField(
                        blank=True,
                        related_name="books",
                        through="core.BookGenre",
                        to="core.genre",
                    ),
                ),
            ],
            database_operations=[],
        ),
        migrations.AddField(
            model_name="bookgenre",
            name="source",
            field=models.CharField(
                choices=[
                    ("provider", "Metadata provider subject"),
                    ("embedded", "Embedded file subject"),
                    ("reviewer", "Reviewer"),
                    ("admin", "Admin"),
                ],
                default="admin",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="bookgenre",
            name="source_tag",
            field=models.CharField(
                blank=True,
                help_text="Subject tag that mapped to this genre, for automatic assignments.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="bookgenre",
            name="created_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AlterModelOptions(
            name="genre",
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="GenreAlias",
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
                    "alias",
                    models.CharField(
                        help_text=(
                            "Subject tag as providers or files write it. Case, accents, "
                            "punctuation and spacing are ignored when matching."
                        ),
                        max_length=255,
                    ),
                ),
                ("key", models.CharField(editable=False, max_length=255, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "genre",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="aliases",
                        to="core.genre",
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "genre aliases",
                "ordering": ["alias"],
            },
        ),
    ]
