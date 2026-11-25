"""Stage results carry an input fingerprint; staleness is derived (#205).

Each successful result records what it was computed from: the predecessor's
``output_sha256`` (the page version's checksum for the first stage), the parameters
of that run and the revision it ran at. Whether a result is current is computed from
these, so the stored ``invalidated`` status and ``invalidated_reason`` go away.

Existing successful rows get the fingerprint of their current inputs, so nothing that
is up to date re-runs. Invalidated rows had no current result: they become
``unknown`` (not run) and keep their old artifacts, which are no longer shown.
"""

from django.db import migrations, models


def forwards(apps, schema_editor):
    states = apps.get_model("core", "ScanTailorStageState")
    versions = apps.get_model("core", "ScanPageVersion")
    alias = schema_editor.connection.alias
    states.objects.using(alias).filter(status="invalidated").update(status="unknown")
    rows = list(
        states.objects.using(alias).only(
            "id",
            "scan_page_id",
            "status",
            "revision",
            "parameters",
            "output_sha256",
            "input_stage_state_id",
            "input_scan_page_version_id",
        )
    )
    by_id = {row.id: row for row in rows}
    version_ids = {row.input_scan_page_version_id for row in rows if row.input_scan_page_version_id}
    page_ids = {
        row.scan_page_id
        for row in rows
        if row.input_stage_state_id is None and row.input_scan_page_version_id is None
    }
    checksums = dict(
        versions.objects.using(alias)
        .filter(id__in=version_ids)
        .values_list("id", "checksum_sha256")
    )
    active = dict(
        versions.objects.using(alias)
        .filter(scan_page_id__in=page_ids, is_active=True)
        .values_list("scan_page_id", "checksum_sha256")
    )
    changed = []
    for row in rows:
        if row.status != "success":
            continue
        if row.input_stage_state_id is not None:
            predecessor = by_id.get(row.input_stage_state_id)
            fingerprint = predecessor.output_sha256 if predecessor is not None else ""
        elif row.input_scan_page_version_id is not None:
            fingerprint = checksums.get(row.input_scan_page_version_id, "")
        else:
            fingerprint = active.get(row.scan_page_id, "")
        row.applied_input_sha256 = fingerprint or ""
        row.applied_parameters = row.parameters or {}
        row.applied_revision = row.revision
        changed.append(row)
    states.objects.using(alias).bulk_update(
        changed,
        ["applied_input_sha256", "applied_parameters", "applied_revision"],
        batch_size=500,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0052_deskew_angle_clockwise"),
    ]

    operations = [
        migrations.AddField(
            model_name="scantailorstagestate",
            name="applied_input_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="scantailorstagestate",
            name="applied_parameters",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="scantailorstagestate",
            name="applied_revision",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="scantailorstagestate",
            name="invalidated_reason",
        ),
        migrations.AlterField(
            model_name="scantailorstagestate",
            name="status",
            field=models.CharField(
                choices=[
                    ("unknown", "Unknown"),
                    ("queued", "Queued"),
                    ("running", "Running"),
                    ("success", "Success"),
                    ("failed", "Failed"),
                ],
                default="unknown",
                max_length=16,
            ),
        ),
    ]
