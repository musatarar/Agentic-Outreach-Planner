"""A row is generated copy awaiting review, not the action it was made for."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0003_action_queue"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="OutreachAction",
            new_name="OutreachGeneratedCopy",
        ),
        # A related_name lives in the model state, not in the database.
        migrations.AlterField(
            model_name="outreachgeneratedcopy",
            name="lead",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="generated_copy",
                to="app.lead",
            ),
        ),
    ]
