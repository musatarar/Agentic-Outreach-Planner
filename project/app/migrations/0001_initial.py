"""The whole schema: seven models, created from scratch.

This replaces the fourteen migrations the strip left behind -- most of them
building subsystems the strip then deleted, so the old chain spent its time
creating tables only to drop them again. It is a hard reset, not a Django
`squashmigrations` (no `replaces`): the project has no deployment anywhere, so
there was no applied history to stay compatible with. A checkout that predates
this commit deletes its `db.sqlite3` and re-migrates.

Regenerated a second time to add `Lead.tenant`, on the same reasoning and with
the same cost: still no deployment, so the column is created with the table
rather than ALTERed in afterwards. That is a decision about this file, not a
relaxation of the rule -- follow-ups are additive unless a human says otherwise,
and any checkout that already migrated deletes its `db.sqlite3` again.

Regenerated a third time to replace that `Lead.tenant` column with `Lead.owner`,
a foreign key to the user whose rules run for the lead. Same reasoning, same
cost, same instruction: still no deployment, so the column is swapped in place
rather than added and backfilled, and a checkout that already migrated deletes
its `db.sqlite3` again.

Regenerated a fourth time, and this one also absorbs `0002_rules_catalog` and
`0003_action_queue`, so the whole schema is one file again: `OutreachAction`
becomes `OutreachGeneratedCopy`, gains the `subject`/`body` pair behind its
draft and the reviewer's own two halves, takes a nullable foreign key to the
catalog action it was generated for, and drops NOT NULL from `priority`.
Owner-approved, on the same reasoning and at the same cost as the three before
it: no deployment, so the table is created in its final shape rather than
renamed and ALTERed four times. Because every database is created with the
parts constraint already in place, there is no window in which a draft can
exist without them, and so nothing to backfill -- the command that did it is
gone with the migrations that needed it. A checkout that already migrated
deletes its `db.sqlite3` again.
"""

from django.conf import settings
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ActionType",
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
                    "key",
                    models.CharField(
                        max_length=64,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[a-z][a-z0-9_]*$",
                                "Use a snake_case key: lowercase letters, digits and underscores, starting with a letter (e.g. 'reward_power_user').",
                            )
                        ],
                    ),
                ),
                ("label", models.CharField(max_length=255)),
                (
                    "description",
                    models.TextField(
                        blank=True,
                        default="",
                        validators=[django.core.validators.MaxLengthValidator(500)],
                    ),
                ),
                (
                    "urgency",
                    models.CharField(
                        choices=[
                            ("low", "Low"),
                            ("medium", "Medium"),
                            ("high", "High"),
                        ],
                        default="medium",
                        max_length=8,
                    ),
                ),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="action_types",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["key"],
            },
        ),
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
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="leads",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="OutreachGeneratedCopy",
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
                ("priority", models.IntegerField(blank=True, null=True)),
                ("action_type", models.CharField(max_length=64)),
                ("reason", models.TextField()),
                ("suggested_copy", models.TextField(blank=True)),
                ("subject", models.CharField(blank=True, default="", max_length=120)),
                ("body", models.TextField(blank=True, default="")),
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
                    "edited_subject",
                    models.CharField(blank=True, default="", max_length=120),
                ),
                ("edited_body", models.TextField(blank=True, default="")),
                (
                    "dedupe_key",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=128
                    ),
                ),
                ("verification", models.JSONField(blank=True, default=dict)),
                (
                    "action",
                    models.ForeignKey(
                        blank=True,
                        db_index=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="app.actiontype",
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="generated_copy",
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
                        to="app.outreachgeneratedcopy",
                    ),
                ),
            ],
            options={
                "ordering": ["-dismissed_at"],
            },
        ),
        migrations.CreateModel(
            name="ActionJob",
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
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("processing", "Processing"),
                            (
                                "deterministic_action_chosen",
                                "Deterministic action chosen",
                            ),
                            ("inferring", "Inferring"),
                            ("inferred_action_chosen", "Inferred action chosen"),
                            ("no_action", "No action"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=32,
                    ),
                ),
                ("decision", models.JSONField(blank=True, default=dict)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "started_at",
                    models.DateTimeField(blank=True, default=None, null=True),
                ),
                (
                    "finished_at",
                    models.DateTimeField(blank=True, default=None, null=True),
                ),
                (
                    "events",
                    models.ManyToManyField(
                        blank=True, related_name="action_jobs", to="app.event"
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="action_jobs",
                        to="app.lead",
                    ),
                ),
                (
                    "selected_action",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="app.actiontype",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.CreateModel(
            name="OutreachRule",
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
                ("name", models.CharField(max_length=255)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("deterministic", "Deterministic"),
                            ("inference", "AI inference"),
                        ],
                        max_length=16,
                    ),
                ),
                ("conditions", models.JSONField(blank=True, default=dict)),
                (
                    "inference_prompt",
                    models.TextField(
                        blank=True,
                        default="",
                        validators=[django.core.validators.MaxLengthValidator(2000)],
                    ),
                ),
                ("enabled", models.BooleanField(default=True)),
                (
                    "weight",
                    models.PositiveSmallIntegerField(
                        choices=[(1, "Low"), (2, "Medium"), (3, "High")], default=2
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "action",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="rules",
                        to="app.actiontype",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="outreach_rules",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-weight", "id"],
                "indexes": [
                    models.Index(
                        fields=["owner", "enabled"], name="orule_owner_enabled"
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="outreachrule",
            constraint=models.CheckConstraint(
                check=models.Q(("kind__in", ("deterministic", "inference"))),
                name="orule_kind_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="outreachrule",
            constraint=models.CheckConstraint(
                check=models.Q(("weight__in", (1, 2, 3))), name="orule_weight_1_to_3"
            ),
        ),
        migrations.AddIndex(
            model_name="outreachgeneratedcopy",
            index=models.Index(
                fields=["status", "priority", "lead"], name="oa_queue_order"
            ),
        ),
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
        migrations.AddConstraint(
            model_name="actiontype",
            constraint=models.UniqueConstraint(
                fields=("owner", "key"), name="atype_one_key_per_owner"
            ),
        ),
        migrations.AddIndex(
            model_name="actionjob",
            index=models.Index(
                fields=["status", "created_at"], name="ajob_queue_order"
            ),
        ),
        migrations.AddConstraint(
            model_name="actionjob",
            constraint=models.CheckConstraint(
                check=models.Q(
                    (
                        "status__in",
                        (
                            "queued",
                            "processing",
                            "deterministic_action_chosen",
                            "inferring",
                            "inferred_action_chosen",
                            "no_action",
                            "failed",
                        ),
                    )
                ),
                name="ajob_status_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="actionjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("status__in", ("queued", "processing", "inferring"))
                ),
                fields=("lead",),
                name="ajob_one_open_per_lead",
            ),
        ),
    ]
