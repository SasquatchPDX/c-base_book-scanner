"""ScanJob.id becomes an opaque string primary key (#126, libreflip contract §4.1/D1).

Steps, in order:

1. ``AlterField``: ``UUIDField`` -> ``CharField(max_length=64)``. Django changes the
   type of every column that references ``core_scanjob.id`` in the same operation
   (``ScanPage``, ``ScanTodo``, ``ScanTailorStageState``, ``MetadataSearch``,
   ``MetadataCandidate``, ``ReviewTask``, ``LLMSuggestion``, ``ContributionEvent``) and
   drops/re-creates their foreign-key constraints.
   * PostgreSQL casts ``uuid::varchar``, which already yields the canonical
     lowercase hyphenated form. The table and every index on the changed columns are
     rewritten under an ACCESS EXCLUSIVE lock (see docs/scanner-protocol.md).
   * SQLite stores UUIDs as 32 hex digits and copies them unchanged when it rebuilds
     the tables, so step 2 rewrites them.
2. ``RunPython``: rewrite every scan job ID (and every referencing column, discovered
   from the model graph) into the canonical ``str(uuid.UUID(...))`` form.
3. ``RunPython``: rewrite ``scan_job_id`` values inside JSON fields into the same
   canonical form. The application has always written ``str(job.id)``, so this is
   normally a no-op; it guards against any non-canonical value.
4. New fields ``ScanJob.device`` and ``ScanJob.scan_started_at``.

Reversal works while every job ID is still a UUID. Once a scanner-assigned ID such as
``20261117T143201Z`` exists, reversing stops with ``IrreversibleError`` instead of
losing jobs.
"""

import uuid
from typing import Any

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
from django.db.models.functions import Cast

import ebook_server.core.scan_job_ids

# JSON fields the application writes ``scan_job_id`` into.
JSON_FIELDS = (
    ("ProcessingJob", "details"),
    ("FileVariant", "converter_options"),
    ("ScanJob", "notes"),
    ("ReviewTask", "decision_payload"),
    ("ContributionEvent", "outcome"),
)
JSON_KEY = "scan_job_id"


def _canonical_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def scan_job_fk_columns(apps: Any) -> list[tuple[str, str]]:
    """``(table, column)`` of every concrete foreign key to ScanJob, in any app."""
    columns = []
    for model in apps.get_models():
        for field in model._meta.local_concrete_fields:
            related = getattr(field, "related_model", None)
            if field.many_to_one and related is not None:
                if related._meta.label_lower == "core.scanjob":
                    columns.append((model._meta.db_table, field.column))
    return sorted(columns)


def _rewrite_scan_job_ids(schema_editor: Any, apps: Any, convert: Any) -> None:
    connection = schema_editor.connection
    quote = connection.ops.quote_name
    table = apps.get_model("core", "ScanJob")._meta.db_table
    references = scan_job_fk_columns(apps)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {quote('id')} FROM {quote(table)}")
        ids = [row[0] for row in cursor.fetchall()]
        for old in ids:
            new = convert(old)
            if new == old:
                continue
            cursor.execute(
                f"UPDATE {quote(table)} SET {quote('id')} = %s WHERE {quote('id')} = %s",
                [new, old],
            )
            for ref_table, ref_column in references:
                cursor.execute(
                    f"UPDATE {quote(ref_table)} SET {quote(ref_column)} = %s "
                    f"WHERE {quote(ref_column)} = %s",
                    [new, old],
                )


def canonicalize_ids(apps: Any, schema_editor: Any) -> None:
    def convert(value: Any) -> str:
        text = str(value)
        return _canonical_uuid(text) or text

    _rewrite_scan_job_ids(schema_editor, apps, convert)


def restore_uuid_ids(apps: Any, schema_editor: Any) -> None:
    connection = schema_editor.connection
    quote = connection.ops.quote_name
    table = apps.get_model("core", "ScanJob")._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {quote('id')} FROM {quote(table)}")
        blocking = [row[0] for row in cursor.fetchall() if _canonical_uuid(row[0]) is None]
    if blocking:
        raise IrreversibleError(
            f"core.0038 cannot be reversed: {len(blocking)} scan job(s) have scanner-assigned "
            f"IDs that are not UUIDs (for example {sorted(blocking)[0]!r})."
        )
    if connection.features.has_native_uuid_field:
        return  # PostgreSQL casts varchar::uuid itself.

    # Non-native backends (SQLite) store UUIDField values as 32 hex digits.
    def convert(value: Any) -> str:
        return uuid.UUID(str(value)).hex

    _rewrite_scan_job_ids(schema_editor, apps, convert)


def _canonical_json(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        result = {}
        for key, item in value.items():
            if key == JSON_KEY and isinstance(item, str):
                canonical = _canonical_uuid(item)
                if canonical is not None and canonical != item:
                    result[key] = canonical
                    changed = True
                    continue
            new_item, item_changed = _canonical_json(item)
            result[key] = new_item
            changed = changed or item_changed
        return result, changed
    if isinstance(value, list):
        changed = False
        items = []
        for item in value:
            new_item, item_changed = _canonical_json(item)
            items.append(new_item)
            changed = changed or item_changed
        return items, changed
    return value, False


def canonicalize_json_references(apps: Any, schema_editor: Any) -> None:
    db_alias = schema_editor.connection.alias
    for model_name, field_name in JSON_FIELDS:
        model = apps.get_model("core", model_name)
        rows = (
            model.objects.using(db_alias)
            .annotate(_json_text=Cast(field_name, models.TextField()))
            .filter(_json_text__contains=JSON_KEY)
            .only("pk", field_name)
        )
        pending = []
        for row in rows.iterator(chunk_size=500):
            value, changed = _canonical_json(getattr(row, field_name))
            if changed:
                setattr(row, field_name, value)
                pending.append(row)
            if len(pending) >= 500:
                model.objects.using(db_alias).bulk_update(pending, [field_name])
                pending = []
        if pending:
            model.objects.using(db_alias).bulk_update(pending, [field_name])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0037_scanner_device"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scanjob",
            name="id",
            field=models.CharField(
                default=ebook_server.core.scan_job_ids.new_scan_job_id,
                editable=False,
                max_length=64,
                primary_key=True,
                serialize=False,
            ),
        ),
        migrations.RunPython(canonicalize_ids, restore_uuid_ids),
        # Canonical strings are what the code writes anyway, so nothing to undo.
        migrations.RunPython(canonicalize_json_references, migrations.RunPython.noop),
        migrations.AddField(
            model_name="scanjob",
            name="device",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="scan_jobs",
                to="core.scannerdevice",
            ),
        ),
        migrations.AddField(
            model_name="scanjob",
            name="scan_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
