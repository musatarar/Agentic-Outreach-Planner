"""The workspace the test suites plan and read in.

Tenancy is now on the read path of every lead, event and outreach query, so a
factory that skips it builds a row no endpoint can see. One shared default
keeps the existing suites' meaning unchanged: everything is in one workspace
unless a test deliberately makes a second one (tests_tenancy.py).
"""

from project.app.models import Tenant

DEFAULT_TENANT_SLUG = "test"
DEFAULT_TENANT_NAME = "Test workspace"


def default_tenant():
    """The suite-wide workspace; created on first use, then reused."""
    return Tenant.objects.get_or_create(
        slug=DEFAULT_TENANT_SLUG, defaults={"name": DEFAULT_TENANT_NAME}
    )[0]
