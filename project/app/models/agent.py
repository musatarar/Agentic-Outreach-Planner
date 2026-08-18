"""Agentic copy-run state: per-lead runs, the step log, synthetic AE calendar (MUS-29)."""

from django.db import models

from .lead import Lead
from .llm import ProviderTrace


class AgentLeadRun(models.Model):
    """The per-lead resume unit of an agentic copy run (MUS-29).

    One row per (trace_run_id, lead). Ownership is taken by an epoch-CAS
    conditional UPDATE: rowcount 1 means the caller owns the run, so racing
    workers produce exactly one winner and dead-worker takeover still works.
    """

    STATUS_PENDING = "pending"
    STATUS_CLAIMED = "claimed"
    STATUS_GATHERING = "gathering"
    STATUS_DRAFTING = "drafting"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_EXHAUSTED = "exhausted"
    NON_TERMINAL_STATUSES = (STATUS_PENDING, STATUS_CLAIMED, STATUS_GATHERING, STATUS_DRAFTING)

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="agent_runs")
    trace_run_id = models.CharField(max_length=36, db_index=True)
    status = models.CharField(max_length=16, default=STATUS_PENDING)
    claimed_by = models.CharField(max_length=32, blank=True, default="")  # worker token (uuid4 hex)
    claim_epoch = models.IntegerField(default=0)  # CAS counter: bumps on every claim
    steps_used = models.IntegerField(default=0)
    tool_calls_used = models.IntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["trace_run_id", "lead"], name="alr_one_run_per_lead_per_trace"
            ),
        ]
        indexes = [
            models.Index(fields=["trace_run_id", "status"], name="alr_resume_scan"),
        ]

    def __str__(self):
        return f"agent run {self.trace_run_id}/{self.lead_id} [{self.status}]"


class AgentStep(models.Model):
    """Append-only step log: crash-resume checkpoint and reasoning trace in one.

    Payload shapes by ``kind`` are pinned in docs/contracts/agent-loop.md.
    """

    lead_run = models.ForeignKey(AgentLeadRun, on_delete=models.CASCADE, related_name="steps")
    seq = models.IntegerField()
    kind = models.CharField(max_length=16)  # llm_call | tool_result | final
    payload = models.JSONField(default=dict)
    # The same `genai.sha256_of` hashes the OTel spans carry (MUS-25), so a span
    # and a step cross-reference without leaking content.
    request_sha256 = models.CharField(max_length=64, blank=True, default="")
    result_sha256 = models.CharField(max_length=64, blank=True, default="")
    # The provider call this step made, for `llm_call` steps only; NULL on
    # `tool_result`/`final` and on every row written before the audit existed.
    provider_trace = models.ForeignKey(
        ProviderTrace, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["seq"]
        constraints = [
            models.UniqueConstraint(fields=["lead_run", "seq"], name="astep_one_seq_per_run"),
        ]

    def __str__(self):
        return f"step {self.seq} ({self.kind}) of run {self.lead_run_id}"


class AEAvailabilitySlot(models.Model):
    """Synthetic calendar slots backing the `check_ae_calendar` tool (MUS-29).

    Seeded only by `ingest_data` (idempotent delete-and-recreate of
    `synthetic=True` rows) -- never written by the agent loop.
    """

    ae_name = models.CharField(max_length=100)
    ae_email = models.EmailField()
    slot_start = models.DateTimeField()
    slot_end = models.DateTimeField()
    synthetic = models.BooleanField(default=True)

    class Meta:
        ordering = ["slot_start"]

    def __str__(self):
        return f"{self.ae_name}: {self.slot_start:%Y-%m-%d %H:%M}"
