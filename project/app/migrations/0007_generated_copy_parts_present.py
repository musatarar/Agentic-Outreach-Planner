"""A draft with no parts behind it is a row no producer can write.

Runs AFTER ``backfill_generated_copy_parts``: rows written before 0006 carry a
composed ``suggested_copy`` and no parts, and this refuses them.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0006_generated_copy_parts"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="outreachgeneratedcopy",
            constraint=models.CheckConstraint(
                check=models.Q(
                    ("suggested_copy", ""),
                    models.Q(
                        models.Q(("subject", ""), _negated=True),
                        models.Q(("body", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="ogc_parts_present_with_copy",
            ),
        ),
    ]
