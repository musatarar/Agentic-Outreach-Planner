"""The catalog action a draft was generated for; an engine draft stores no
priority of its own.

One migration for both: nothing filters on ``action`` (the row joins out to it),
so this is a bare nullable ADD COLUMN plus a DROP NOT NULL, and one brief
ACCESS EXCLUSIVE window on a hot table beats two.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0004_rename_generated_copy"),
    ]

    operations = [
        migrations.AddField(
            model_name="outreachgeneratedcopy",
            name="action",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="app.actiontype",
            ),
        ),
        migrations.AlterField(
            model_name="outreachgeneratedcopy",
            name="priority",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
