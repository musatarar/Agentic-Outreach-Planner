from django.apps import AppConfig


class AppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "project.app"

    def ready(self):
        from project.app import checks  # noqa: F401 -- import registers the check

        # Telemetry bootstrap (MUS-25). Here rather than in settings.py, which
        # is imported before the app registry exists; and rather than wrapping
        # the entrypoint in `opentelemetry-instrument`, which would give the
        # Docker and `runserver` paths different startup sequences. A no-op
        # unless OTEL_EXPORTER_OTLP_ENDPOINT is set, and idempotent under the
        # runserver autoreloader -- see services/telemetry/setup.py.
        from project.app.services import telemetry

        telemetry.configure_from_env()
