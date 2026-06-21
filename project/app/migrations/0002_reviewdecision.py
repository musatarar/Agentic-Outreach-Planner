from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReviewDecision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(max_length=32)),
                ("status", models.CharField(max_length=32)),
                ("selected_action_type", models.CharField(blank=True, max_length=64)),
                ("proposed_name", models.CharField(blank=True, max_length=255)),
                ("proposed_what", models.TextField(blank=True)),
                ("proposed_when", models.TextField(blank=True)),
                ("reviewer", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "outreach_action",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="review_decisions",
                        to="app.outreachaction",
                    ),
                ),
            ],
        ),
    ]
