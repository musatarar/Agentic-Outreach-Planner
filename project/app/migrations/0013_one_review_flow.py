"""One review flow: drop snooze, the undo window's bookkeeping and both
decision tables.

`OutreachAction` keeps the copy a reviewer reads and decides on; the state
machine narrows to pending -> approved | dismissed, both reversible. The
dismissal reason now lives only on `DismissedOutreachKey`, which stays: it is
why a re-run does not resurrect a dismissed recommendation.

Hand-ordered rather than left as `makemigrations` emitted it. The autodetector
leads with `RemoveField` on `reviewdecision.outreach_action`, which SQLite
serves by rebuilding the table -- and the rebuild re-reads a model whose
`rd_one_resolution_per_action` / `rd_one_live_send_per_action` constraints name
the field being removed, so it dies. Dropping both tables whole, before
touching `outreachaction`, needs no rebuild and takes the constraints with
them. Same end state; `makemigrations --check` stays clean.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0012_drop_trace_run_id"),
    ]

    operations = [
        # Referents first: both tables carry an FK to outreachaction.
        migrations.DeleteModel(name="OutreachEdit"),
        migrations.DeleteModel(name="ReviewDecision"),
        migrations.RemoveField(model_name="outreachaction", name="snooze_until"),
        migrations.RemoveField(model_name="outreachaction", name="snooze_trigger"),
        migrations.RemoveField(model_name="outreachaction", name="snooze_activity_after"),
        migrations.RemoveField(model_name="outreachaction", name="dismiss_reason"),
        migrations.RemoveField(model_name="outreachaction", name="rule_trace"),
        migrations.AlterField(
            model_name="outreachaction",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("approved", "Approved"),
                    ("dismissed", "Dismissed"),
                ],
                db_index=True,
                default="pending",
                max_length=16,
            ),
        ),
    ]
