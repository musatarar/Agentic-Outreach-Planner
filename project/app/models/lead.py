"""The lead record and its ingested activity events."""

from django.conf import settings
from django.db import models


def scope_to_tenant(queryset, user, field="tenant"):
    """Narrow ``queryset`` to what ``user`` may see. The one definition of the rule.

    Visible: the rows this user owns, plus the ones no user owns yet.
    ``Lead.tenant`` is nullable while the book is being backfilled (see
    ``manage.py backfill_lead_tenant``), and a NULL row belongs to nobody, so it
    stays visible to every signed-in reviewer rather than to none of them. Once
    the backfill has run everywhere, dropping the ``__isnull`` arm and
    constraining the column turns this into hard isolation -- a reviewed,
    human-gated change, not a drive-by one.

    ``field`` is the path to the tenant column, so models that reach a Lead
    through a relation share this rule instead of restating it.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return queryset.none()
    return queryset.filter(models.Q(**{field: user}) | models.Q(**{f"{field}__isnull": True}))


class LeadQuerySet(models.QuerySet):
    def for_tenant(self, user):
        """The leads ``user`` may see. See :func:`scope_to_tenant`."""
        return scope_to_tenant(self, user)


class Lead(models.Model):
    id = models.CharField(max_length=32, primary_key=True)  # "lead_001"
    # The owning user: this book's tenant. NULL means not yet assigned -- see
    # `LeadQuerySet.for_tenant`. PROTECT, not CASCADE: deleting a user must not
    # silently take a book of leads (and its outreach audit trail) with it.
    tenant = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="leads",
        null=True,
        blank=True,
    )
    agency_name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255)
    contact_email = models.EmailField()
    contact_phone = models.CharField(max_length=32)
    state = models.CharField(max_length=2)
    num_producers = models.IntegerField()
    years_in_business = models.IntegerField()
    estimated_book_size_usd = models.BigIntegerField()
    stage = models.CharField(max_length=32)  # "active_trial" | "demo_completed"
    signed_up_date = models.DateField(null=True)
    last_login_date = models.DateField(null=True)
    quotes_created = models.IntegerField(default=0)
    quotes_submitted = models.IntegerField(default=0)
    deals_closed = models.IntegerField(default=0)
    last_contacted_date = models.DateField(null=True)
    hubspot_notes = models.TextField(blank=True)

    objects = LeadQuerySet.as_manager()

    def __str__(self):
        return f"{self.id} - {self.agency_name}"


class Event(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="events")
    type = models.CharField(max_length=32)  # login, quote_created, quote_submitted,
    # deal_closed, call_logged, email_sent,
    # demo_completed, onboarding_call
    timestamp = models.DateTimeField()
    meta = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.lead_id} - {self.type} @ {self.timestamp:%Y-%m-%d}"
