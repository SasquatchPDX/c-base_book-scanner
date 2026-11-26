"""#219: model a page's disposition as ``normal | blank | excluded``.

0054 stored the blank declaration as a boolean. The issue asks for one field with
distinct values so that ``excluded`` (not part of the book) can reuse the plumbing
later without overloading "blank". Existing declarations become ``blank``; the
audit events keep their history as dispositions.
"""

import django.db.models.deletion
from django.db import migrations, models

DISPOSITIONS = [("normal", "Normal"), ("blank", "Blank"), ("excluded", "Excluded")]


def _forward(apps, schema_editor):
    ScanPage = apps.get_model("core", "ScanPage")
    ScanPage.objects.filter(declared_blank=True).update(page_disposition="blank")
    Event = apps.get_model("core", "ScanPageDispositionEvent")
    Event.objects.filter(previous_value=True).update(previous_disposition="blank")
    Event.objects.filter(declared_blank=True).update(disposition="blank")


def _backward(apps, schema_editor):
    ScanPage = apps.get_model("core", "ScanPage")
    ScanPage.objects.filter(page_disposition="blank").update(declared_blank=True)
    Event = apps.get_model("core", "ScanPageDispositionEvent")
    Event.objects.filter(previous_disposition="blank").update(previous_value=True)
    Event.objects.filter(disposition="blank").update(declared_blank=True)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0055_scantailor_run_all"),
    ]

    operations = [
        migrations.AddField(
            model_name="scanpage",
            name="page_disposition",
            field=models.CharField(choices=DISPOSITIONS, default="normal", max_length=16),
        ),
        migrations.RenameModel(old_name="ScanPageBlankEvent", new_name="ScanPageDispositionEvent"),
        migrations.AddField(
            model_name="scanpagedispositionevent",
            name="previous_disposition",
            field=models.CharField(choices=DISPOSITIONS, default="normal", max_length=16),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="scanpagedispositionevent",
            name="disposition",
            field=models.CharField(choices=DISPOSITIONS, default="normal", max_length=16),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="scanpagedispositionevent",
            name="reason",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AlterField(
            model_name="scanpagedispositionevent",
            name="previous_value",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="scanpagedispositionevent",
            name="declared_blank",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(_forward, _backward),
        migrations.RemoveField(model_name="scanpage", name="declared_blank"),
        migrations.RemoveField(model_name="scanpagedispositionevent", name="previous_value"),
        migrations.RemoveField(model_name="scanpagedispositionevent", name="declared_blank"),
        migrations.AlterField(
            model_name="scanpagedispositionevent",
            name="page",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="disposition_events",
                to="core.scanpage",
            ),
        ),
    ]
