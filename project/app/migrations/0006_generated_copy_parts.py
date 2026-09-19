"""The subject and body behind a draft, generated and edited.

Blank-default ADD COLUMNs: existing rows keep their composed ``suggested_copy``
and carry no parts until ``backfill_generated_copy_parts`` splits them, which
``0007`` then constrains.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0005_generated_copy_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="outreachgeneratedcopy",
            name="subject",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="outreachgeneratedcopy",
            name="body",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="outreachgeneratedcopy",
            name="edited_subject",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="outreachgeneratedcopy",
            name="edited_body",
            field=models.TextField(blank=True, default=""),
        ),
    ]
