"""Add the nullable Lead.tenant FK.

HOT TABLE: `lead` is one of the three tables CLAUDE.md calls out, and an FK
carries an index, so on Postgres this add takes locks that a plain AddField
does not release until it finishes. The project has no deployment (see the
0001 docstring), so this stays a plain AddField rather than an `atomic = False`
concurrent-index dance that would not run on SQLite -- and the PR flags it for
human review, as the rules require.

Nullable first (CLAUDE.md): rows are backfilled by the `backfill_tenant`
management command, and a follow-up migration constrains the column.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0003_tenantmembership"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="leads",
                to="app.tenant",
            ),
        ),
    ]
