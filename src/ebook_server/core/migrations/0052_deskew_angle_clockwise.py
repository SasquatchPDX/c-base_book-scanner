"""Deskew angles become clockwise-positive (#195 step 4).

Before this migration the Deskew stage stored ``manual_angle`` and ``angle`` in Pillow's
counter-clockwise convention. Issue #195 (section 6.1) asks for positive = clockwise,
which is also upstream Scan Tailor's internal convention. Both values are negated, so
every page keeps exactly the rotation it had. The recorded ``transform`` op is not
touched: ``scan_transform``'s ``rotate`` op stays counter-clockwise, and the stage now
records ``rotate(-angle)``.

The old auto-search parameters (``angle_min``, ``angle_max``, ``angle_step``) are
dropped: the SkewFinder port searches upstream's fixed range of +-7 degrees.
Stage statuses and revisions are unchanged; nothing needs to re-run.
"""

from django.db import migrations

LEGACY_KEYS = ("angle_min", "angle_max", "angle_step")


def _flip(apps, schema_editor, *, drop_legacy: bool) -> None:
    states = apps.get_model("core", "ScanTailorStageState")
    alias = schema_editor.connection.alias
    changed = []
    for state in states.objects.using(alias).filter(stage="deskew").only("id", "parameters"):
        parameters = dict(state.parameters or {})
        touched = False
        for key in ("manual_angle", "angle"):
            value = parameters.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                parameters[key] = -value + 0.0
                touched = True
        if drop_legacy:
            for key in LEGACY_KEYS:
                if key in parameters:
                    del parameters[key]
                    touched = True
        if touched:
            state.parameters = parameters
            changed.append(state)
    states.objects.using(alias).bulk_update(changed, ["parameters"], batch_size=500)


def forwards(apps, schema_editor):
    _flip(apps, schema_editor, drop_legacy=True)


def backwards(apps, schema_editor):
    _flip(apps, schema_editor, drop_legacy=False)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0051_stage_state_job_updated_index"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
