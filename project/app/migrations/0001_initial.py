"""The whole schema: five models, created from scratch.

This replaces the fourteen migrations the strip left behind -- most of them
building subsystems the strip then deleted, so the old chain spent its time
creating tables only to drop them again. It is a hard reset, not a Django
`squashmigrations` (no `replaces`): the project has no deployment anywhere, so
there was no applied history to stay compatible with. A checkout that predates
this commit deletes its `db.sqlite3` and re-migrates.

Additive follow-up migrations from here; this file is history now.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Lead",
            fields=[
                (
                    "id",
                    models.CharField(max_length=32, primary_key=True, serialize=False),
                ),
                ("agency_name", models.CharField(max_length=255)),
                ("contact_name", models.CharField(max_length=255)),
                ("contact_email", models.EmailField(max_length=254)),
                ("contact_phone", models.CharField(max_length=32)),
                ("state", models.CharField(max_length=2)),
                ("num_producers", models.IntegerField()),
                ("years_in_business", models.IntegerField()),
                ("estimated_book_size_usd", models.BigIntegerField()),
                ("stage", models.CharField(max_length=32)),
                ("signed_up_date", models.DateField(null=True)),
                ("last_login_date", models.DateField(null=True)),
                ("quotes_created", models.IntegerField(default=0)),
                ("quotes_submitted", models.IntegerField(default=0)),
                ("deals_closed", models.IntegerField(default=0)),
                ("last_contacted_date", models.DateField(null=True)),
                ("hubspot_notes", models.TextField(blank=True)),
            ],
        ),
        migrations.CreateModel(
            name="OutreachAction",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("priority", models.IntegerField()),
                ("action_type", models.CharField(max_length=64)),
                ("reason", models.TextField()),
                ("suggested_copy", models.TextField(blank=True)),
                ("needs_human", models.BooleanField(default=False)),
                ("further_action", models.TextField(blank=True)),
                (
                    "status",
                    models.CharField(
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
                (
                    "status_changed_at",
                    models.DateTimeField(blank=True, default=None, null=True),
                ),
                ("edited_copy", models.TextField(blank=True, default="")),
                (
                    "dedupe_key",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=128
                    ),
                ),
                ("verification", models.JSONField(blank=True, default=dict)),
                (
                    "lead",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="outreach_actions",
                        to="app.lead",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="LoginToken",
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
                ("email", models.EmailField(db_index=True, max_length=254)),
                (
                    "token_hash",
                    models.CharField(db_index=True, max_length=64, unique=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                (
                    "consumed_at",
                    models.DateTimeField(blank=True, default=None, null=True),
                ),
                ("requested_ip", models.GenericIPAddressField(blank=True, null=True)),
                (
                    "requested_user_agent",
                    models.CharField(blank=True, default="", max_length=255),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["email", "-created_at"], name="logintoken_email_recent"
                    ),
                    models.Index(
                        fields=["expires_at", "consumed_at"], name="logintoken_sweep"
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Event",
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
                ("type", models.CharField(max_length=32)),
                ("timestamp", models.DateTimeField()),
                ("meta", models.JSONField(blank=True, default=dict)),
                (
                    "lead",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="app.lead",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="DismissedOutreachKey",
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
                (
                    "dedupe_key",
                    models.CharField(db_index=True, max_length=128, unique=True),
                ),
                ("action_type", models.CharField(max_length=64)),
                ("reason", models.CharField(blank=True, default="", max_length=64)),
                ("dismissed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "dismissed_by",
                    models.EmailField(blank=True, default="", max_length=254),
                ),
                (
                    "revoked_at",
                    models.DateTimeField(blank=True, default=None, null=True),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dismissed_keys",
                        to="app.lead",
                    ),
                ),
                (
                    "source_action",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="app.outreachaction",
                    ),
                ),
            ],
            options={
                "ordering": ["-dismissed_at"],
            },
        ),
        migrations.AddIndex(
            model_name="outreachaction",
            index=models.Index(
                fields=["status", "priority", "lead"], name="oa_queue_order"
            ),
        ),
    ]
