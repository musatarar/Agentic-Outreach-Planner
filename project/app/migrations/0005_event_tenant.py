"""Add the nullable Event.tenant FK (denormalized from the lead).

HOT TABLE: same caveat as 0004 -- `event` is a hot table and this add carries
an index; flagged for human review. Nullable first, backfilled by the
`backfill_tenant` command, constrained by a follow-up migration.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0004_lead_tenant"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="events",
                to="app.tenant",
            ),
        ),
    ]
