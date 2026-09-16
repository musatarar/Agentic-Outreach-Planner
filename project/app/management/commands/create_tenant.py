"""Create a workspace. Idempotent: re-running renames rather than duplicating."""

from django.core.management.base import BaseCommand

from project.app.services.tenancy import create_tenant


class Command(BaseCommand):
    help = "Create a workspace (tenant), or update its name. Idempotent on slug."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Workspace slug, e.g. 'demo'.")
        parser.add_argument("--name", default="", help="Human-readable workspace name.")

    def handle(self, *args, **options):
        slug = options["slug"]
        tenant = create_tenant(slug, options["name"] or slug)
        self.stdout.write(self.style.SUCCESS(f'Workspace "{tenant.slug}" is {tenant.name}.'))
