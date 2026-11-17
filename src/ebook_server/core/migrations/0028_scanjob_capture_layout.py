from django.db import migrations, models


class Migration(migrations.Migration):
    """Add ``ScanJob.capture_layout`` (#55).

    Jobs that already exist keep the previous Page Split behaviour (gutter detection
    on landscape images), so they are added as ``spread``; new jobs default to
    ``single_page``, which is what the project's scanner produces.
    """

    dependencies = [
        ("core", "0027_seed_genres"),
    ]

    operations = [
        migrations.AddField(
            model_name="scanjob",
            name="capture_layout",
            field=models.CharField(
                choices=[
                    ("single_page", "One page per image"),
                    ("spread", "Two facing pages per image"),
                ],
                default="spread",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="scanjob",
            name="capture_layout",
            field=models.CharField(
                choices=[
                    ("single_page", "One page per image"),
                    ("spread", "Two facing pages per image"),
                ],
                default="single_page",
                max_length=16,
            ),
        ),
    ]
