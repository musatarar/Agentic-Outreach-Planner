"""The lead record and its ingested activity events."""

from django.db import models

from .tenancy import Tenant


class Lead(models.Model):
    id = models.CharField(max_length=32, primary_key=True)  # "lead_001"
    # The workspace that owns this lead. Nullable while deployments backfill
    # (see the backfill_tenant command); application code always sets it, and a
    # NULL row matches no tenant filter, so it is invisible to the API.
    # PROTECT: deleting a workspace that still owns leads is an explicit decision.
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, on_delete=models.PROTECT, related_name="leads"
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

    def __str__(self):
        return f"{self.id} - {self.agency_name}"


class Event(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="events")
    # Denormalized from ``lead.tenant`` so an event query can be scoped without
    # a join (the admin and any future direct event ingestion need it).
    # INVARIANT, service-level: ``event.tenant == event.lead.tenant``. A CHECK
    # constraint cannot reference another table, so every writer -- ingest, the
    # backfill command, the test factories -- maintains it.
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, on_delete=models.PROTECT, related_name="events"
    )
    type = models.CharField(max_length=32)  # login, quote_created, quote_submitted,
    # deal_closed, call_logged, email_sent,
    # demo_completed, onboarding_call
    timestamp = models.DateTimeField()
    meta = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.lead_id} - {self.type} @ {self.timestamp:%Y-%m-%d}"
