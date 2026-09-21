"""The user-declared shape replaces the fixed Lead and Event columns.

Hot-table DDL: every ALTER here takes ACCESS EXCLUSIVE on `lead` or `event` on
Postgres. The new blobs are added before the old columns are dropped, so the
table is never without somewhere to put a row.

The dropped values are NOT moved into `data` — seeding is a management
command's job, never a migration's — so an existing database comes out of this
with empty blobs. Re-run `ingest_data` and `seed_rules_catalog` after it: the
first refills the blobs, the second declares the shape that reads them and
rewrites the rules against it.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("app", "0003_action_queue"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="data",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="lead",
            name="data",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RemoveField(
            model_name="event",
            name="meta",
        ),
        migrations.RemoveField(
            model_name="event",
            name="type",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="agency_name",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="contact_email",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="contact_name",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="contact_phone",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="deals_closed",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="estimated_book_size_usd",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="hubspot_notes",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="last_contacted_date",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="last_login_date",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="num_producers",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="quotes_created",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="quotes_submitted",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="signed_up_date",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="stage",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="state",
        ),
        migrations.RemoveField(
            model_name="lead",
            name="years_in_business",
        ),
        migrations.CreateModel(
            name="Shape",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("lead_columns", models.JSONField(blank=True, default=list)),
                ("event_columns", models.JSONField(blank=True, default=list)),
                ("roles", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="shape",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
