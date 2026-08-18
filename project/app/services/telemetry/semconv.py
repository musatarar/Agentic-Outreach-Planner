"""Attribute and metric names used by this app's telemetry, spelled out once.

Three namespaces: ``gen_ai.*``/``server.*``/``error.type`` from OTel semconv
v1.40.0, ``llm.*``/``openinference.*`` from OpenInference (Phoenix does not read
``gen_ai.*``, so values are dual-emitted), and ``outreach.*`` for everything the
spec does not define. Names are hardcoded rather than imported: the packaged
GenAI constants live in a private, deprecated module, and a header naming the
targeted spec version is greppable when the conventions move.
"""

# ---------------------------------------------------------------------------
# OpenTelemetry GenAI semantic conventions (semconv v1.40.0, Development)
# ---------------------------------------------------------------------------

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"

GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"

GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
# An ARRAY of strings, not a string.
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"

GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# Required attribute on gen_ai.client.token.usage; values below.
GEN_AI_TOKEN_TYPE = "gen_ai.token.type"
TOKEN_TYPE_INPUT = "input"
TOKEN_TYPE_OUTPUT = "output"

# gen_ai.operation.name allowed values; only the three we use are named.
OPERATION_CHAT = "chat"
OPERATION_INVOKE_AGENT = "invoke_agent"
OPERATION_EXECUTE_TOOL = "execute_tool"

# Conditionally required on an execute_tool span (MUS-29).
GEN_AI_TOOL_NAME = "gen_ai.tool.name"

# gen_ai.provider.name allowed values, for the four providers this app ships.
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_GROQ = "groq"
PROVIDER_DEEPSEEK = "deepseek"

# Metric instruments (semconv v1.40.0). Both Histograms; both need explicit
# bucket boundaries, since the SDK defaults suit neither seconds nor tokens.
METRIC_OPERATION_DURATION = "gen_ai.client.operation.duration"
METRIC_OPERATION_DURATION_UNIT = "s"
METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
METRIC_TOKEN_USAGE_UNIT = "{token}"

# ---------------------------------------------------------------------------
# General OTel conventions (stable)
# ---------------------------------------------------------------------------

# Conditionally required on a failed span: error class name, never the message.
ERROR_TYPE = "error.type"

SERVER_ADDRESS = "server.address"
SERVER_PORT = "server.port"

# ---------------------------------------------------------------------------
# OpenInference (Arize Phoenix). Dual-emitted alongside the gen_ai.* keys.
# ---------------------------------------------------------------------------

OPENINFERENCE_SPAN_KIND = "openinference.span.kind"
OPENINFERENCE_KIND_AGENT = "AGENT"
OPENINFERENCE_KIND_CHAIN = "CHAIN"
OPENINFERENCE_KIND_LLM = "LLM"
OPENINFERENCE_KIND_TOOL = "TOOL"

LLM_MODEL_NAME = "llm.model_name"
LLM_PROVIDER = "llm.provider"
LLM_TOKEN_COUNT_PROMPT = "llm.token_count.prompt"
LLM_TOKEN_COUNT_COMPLETION = "llm.token_count.completion"
LLM_TOKEN_COUNT_TOTAL = "llm.token_count.total"

# DELIBERATELY NOT EMITTED: OpenInference's prompt and completion carriers,
# which would put lead notes and generated copy into the trace backend. Named
# here so the prohibition is greppable and testable.
FORBIDDEN_CONTENT_KEYS = (
    "llm.input_messages",
    "llm.output_messages",
    "input.value",
    "output.value",
)

# ---------------------------------------------------------------------------
# outreach.* -- this application's own namespace
# ---------------------------------------------------------------------------

# Run scope.
RUN_ID = "outreach.run.id"
LEAD_COUNT = "outreach.lead.count"
NEEDS_HUMAN_COUNT = "outreach.needs_human.count"
CONCURRENCY_MAX_IN_FLIGHT = "outreach.concurrency.max_in_flight"
VERIFY_LEVEL = "outreach.verify.level"

# Lead scope.
LEAD_ID = "outreach.lead.id"
ACTION_TYPE = "outreach.action.type"
ACTION_PRIORITY = "outreach.action.priority"
NEEDS_HUMAN = "outreach.needs_human"
VERIFY_OUTCOME = "outreach.verify.outcome"
VERIFY_VIOLATION_COUNT = "outreach.verify.violation_count"
SHAPE_PROBLEM_COUNT = "outreach.shape.problem_count"
LLM_ATTEMPTS = "outreach.llm.attempts"
# The exception class that ended this lead's work, and whose problem it is
# ("provider" | "configuration" | "contract" | "unknown"), declared in
# ``llm/errors.py``.
FAILURE_KIND = "outreach.failure.kind"
FAILURE_DOMAIN = "outreach.failure.domain"

# Provider-call scope. `outreach.llm.provider` carries OUR configured provider
# name and is emitted even alongside gen_ai.provider.name, so the join back to
# an LLMConfiguration row does not depend on the spec enum having a member.
LLM_PROVIDER_CONFIGURED = "outreach.llm.provider"
LLM_ATTEMPT = "outreach.llm.attempt"
LLM_RETRY_AFTER_S = "outreach.llm.retry_after_s"

# Content references: which record was read and written, plus a digest of each,
# never the text.
INPUT_REF = "outreach.input.ref"
INPUT_SHA256 = "outreach.input.sha256"
OUTPUT_REF = "outreach.output.ref"
OUTPUT_SHA256 = "outreach.output.sha256"

# outreach.verify.outcome allowed values. "skipped" is a decision (no automated
# pattern matched); "not_generated" is a failure (no text was produced).
VERIFY_PASS = "pass"
VERIFY_SHAPE_FAILED = "shape_failed"
VERIFY_GROUNDING_FAILED = "grounding_failed"
VERIFY_BOTH_FAILED = "both_failed"
VERIFY_SKIPPED = "skipped"
VERIFY_NOT_GENERATED = "not_generated"

VERIFY_OUTCOMES = (
    VERIFY_PASS,
    VERIFY_SHAPE_FAILED,
    VERIFY_GROUNDING_FAILED,
    VERIFY_BOTH_FAILED,
    VERIFY_SKIPPED,
    VERIFY_NOT_GENERATED,
)
