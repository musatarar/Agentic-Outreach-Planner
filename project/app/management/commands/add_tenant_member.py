"""Enrol one or more addresses in a workspace.

The Django user is created if it does not exist, by the same helper the
sign-in view uses. An address already in a different workspace is refused: a
silent move would change what that person can see.
"""

from django.core.management.base import BaseCommand, CommandError

from project.app.models import Tenant
from project.app.services.tenancy import add_member


class Command(BaseCommand):
    help = "Add one or more users (by email) to a workspace. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Workspace slug.")
        parser.add_argument("emails", nargs="+", help="One or more email addresses.")

    def handle(self, *args, **options):
        slug = options["slug"]
        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f'No workspace with slug "{slug}".')

        for email in options["emails"]:
            normalized = email.strip().lower()
            try:
                add_member(tenant, normalized)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(self.style.SUCCESS(f'{normalized} is in "{tenant.slug}".'))
