"""Store a book's publication year instead of a full date.

Providers only supply a (first-)publication year; the old DateField stored it as
1 January, which looked like a precise date. Existing values keep their year.
"""

import django.core.validators
from django.db import migrations, models


def date_to_year(apps, schema_editor):  # type: ignore[no-untyped-def]
    Book = apps.get_model("core", "Book")
    for book in Book.objects.exclude(publication_date=None).only("id", "publication_date"):
        Book.objects.filter(pk=book.pk).update(publication_year=book.publication_date.year)


def year_to_date(apps, schema_editor):  # type: ignore[no-untyped-def]
    import datetime

    Book = apps.get_model("core", "Book")
    for book in Book.objects.exclude(publication_year=None).only("id", "publication_year"):
        Book.objects.filter(pk=book.pk).update(
            publication_date=datetime.date(book.publication_year, 1, 1)
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_source_location_path_layout"),
    ]

    operations = [
        migrations.AddField(
            model_name="book",
            name="publication_year",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(9999),
                ],
            ),
        ),
        migrations.RunPython(date_to_year, year_to_date),
        migrations.RemoveField(model_name="book", name="publication_date"),
    ]
