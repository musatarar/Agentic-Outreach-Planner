from django.db import models


class Lead(models.Model):
    id = models.CharField(max_length=32, primary_key=True)  # "lead_001"
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
    type = models.CharField(max_length=32)  # login, quote_created, quote_submitted,
    # deal_closed, call_logged, email_sent,
    # demo_completed, onboarding_call
    timestamp = models.DateTimeField()
    meta = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.lead_id} - {self.type} @ {self.timestamp:%Y-%m-%d}"


class OutreachAction(models.Model):  # what the planner decided/did
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="outreach_actions")
    created_at = models.DateTimeField(auto_now_add=True)
    priority = models.IntegerField()  # 1 highest, 3 lowest
    action_type = models.CharField(max_length=64)  # see ACTION_TYPES
    reason = models.TextField()  # why this lead, why now
    suggested_copy = models.TextField(blank=True)  # Claude-generated email/message
    needs_human = models.BooleanField(default=False)  # unknown action -> report to BD
    further_action = models.TextField(blank=True)  # what ops/AE should do next

    def __str__(self):
        return f"{self.lead_id} - {self.action_type} (p{self.priority})"


class ReviewDecision(models.Model):
    KIND_SELECT = "select_existing"
    KIND_PROPOSE = "propose_new"
    STATUS_RESOLVED = "resolved"
    STATUS_PENDING = "pending_engineering"

    # OneToOne: an action is resolved by exactly one decision; the DB-level
    # unique constraint blocks duplicate/racing submissions for the same action.
    outreach_action = models.OneToOneField(
        OutreachAction, on_delete=models.CASCADE, related_name="review_decision"
    )
    kind = models.CharField(max_length=32)  # select_existing | propose_new
    status = models.CharField(max_length=32)  # resolved | pending_engineering
    selected_action_type = models.CharField(max_length=64, blank=True)
    proposed_name = models.CharField(max_length=255, blank=True)
    proposed_what = models.TextField(blank=True)
    proposed_when = models.TextField(blank=True)
    reviewer = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
