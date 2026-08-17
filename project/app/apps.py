from django.apps import AppConfig


class AppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "project.app"

    def ready(self):
        from project.app import checks  # noqa: F401 -- import registers the check

        # Telemetry bootstrap (MUS-25): no-op unless OTEL_EXPORTER_OTLP_ENDPOINT
        # is set, idempotent under the autoreloader (see services/telemetry/setup.py).
        from project.app.services import telemetry

        telemetry.configure_from_env()
