from django.apps import AppConfig


class AppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "project.app"

    def ready(self):
        from project.app import checks  # noqa: F401 -- import registers the check
