"""Create TenantMembership: the one row that resolves a user to a workspace.

Depends on the swappable user model, so a project that swaps AUTH_USER_MODEL
still orders this after that model's own migration.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("app", "0002_tenant"),
    ]

    operations = [
        migrations.CreateModel(
            name="TenantMembership",
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
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="app.tenant",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tenant_membership",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
