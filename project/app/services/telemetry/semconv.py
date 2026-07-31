"""Attribute and metric names used by this app's telemetry, spelled out once.

Two namespaces live here and the split is the whole point of the module.

**``gen_ai.*`` / ``server.*`` / ``error.type`` are not ours.** They come from the
OpenTelemetry semantic conventions, targeted at:

* OTel semantic conventions **v1.40.0** (GenAI group status: *Development*)
* https://github.com/open-telemetry/semantic-conventions-genai

They are hardcoded strings rather than imports on purpose. The published
``opentelemetry-semantic-conventions==0.65b0`` package does still ship the GenAI
constants, but only under ``opentelemetry.semconv._incubating.attributes`` — a
**private** module — and every constant in it carries the docstring
*"Deprecated: Moved to the OpenTelemetry GenAI semantic conventions
repository."* Importing a private, deprecated symbol to avoid typing a string
buys nothing and breaks on a patch release. Copying the strings under a header
that names the spec version makes the version we target explicit and greppable,
which is what actually matters when the conventions move again.

**``outreach.*`` is ours**, and everything the spec does not define goes there.
This is not tidiness. Squatting on ``gen_ai.*`` for keys the spec has not
defined risks a future release giving those exact keys different semantics, and
some processors (Phoenix's, notably) already pattern-match the namespace. The
rule is one line long: if the spec does not define it, it is ``outreach.*``.

**``llm.*`` / ``openinference.*`` are OpenInference's** (Arize Phoenix's
conventions). Self-hosted Phoenix does not read ``gen_ai.*`` at all
(Arize-ai/phoenix#10622), so the same values are emitted under both namespaces —
see :mod:`project.app.services.telemetry.genai`. Note which OpenInference keys
are deliberately *absent* below.
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
# An ARRAY of strings, not a string. The plural in the key is the spec's, and a
# bare string here is the single easiest way to emit a technically-invalid span
# that still looks right in a UI.
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"

GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# Required attribute on gen_ai.client.token.usage; values below.
GEN_AI_TOKEN_TYPE = "gen_ai.token.type"
TOKEN_TYPE_INPUT = "input"
TOKEN_TYPE_OUTPUT = "output"

# gen_ai.operation.name allowed values (the spec's enum). Only the two we use
# are named; the rest of the enum is chat, create_agent, embeddings,
# execute_tool, generate_content, invoke_workflow, retrieval, text_completion.
OPERATION_CHAT = "chat"
OPERATION_INVOKE_AGENT = "invoke_agent"

# gen_ai.provider.name allowed values, for the four providers this app ships.
# The full enum also includes aws.bedrock, azure.ai.inference, azure.ai.openai,
# cohere, gcp.gemini, gcp.gen_ai, gcp.vertex_ai, ibm.watsonx.ai, mistral_ai,
# perplexity and x_ai.
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_GROQ = "groq"
PROVIDER_DEEPSEEK = "deepseek"

# Metric instruments (semconv v1.40.0). Both are Histograms and both need
# explicit bucket boundaries -- the SDK's default buckets are wrong for a
# seconds-scale latency and very wrong for a token count.
METRIC_OPERATION_DURATION = "gen_ai.client.operation.duration"
METRIC_OPERATION_DURATION_UNIT = "s"
METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
METRIC_TOKEN_USAGE_UNIT = "{token}"

# ---------------------------------------------------------------------------
# General OTel conventions (stable)
# ---------------------------------------------------------------------------

# Conditionally required on a span that failed: the fully-qualified class name
# of the error, or a low-cardinality label for it.
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

LLM_MODEL_NAME = "llm.model_name"
LLM_PROVIDER = "llm.provider"
LLM_TOKEN_COUNT_PROMPT = "llm.token_count.prompt"
LLM_TOKEN_COUNT_COMPLETION = "llm.token_count.completion"
LLM_TOKEN_COUNT_TOTAL = "llm.token_count.total"

# DELIBERATELY NOT EMITTED, and named here so the prohibition is greppable and
# so a test can assert on them by name. These are OpenInference's prompt and
# completion carriers: setting either would put the lead's HubSpot notes and the
# generated email body into the trace backend, defeating the content-reference
# policy the outreach.input.*/outreach.output.* keys below exist to implement.
# See project/app/tests_telemetry_content.py.
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
FAILURE_KIND = "outreach.failure.kind"

# Provider-call scope. `outreach.llm.provider` carries OUR configured provider
# name ("groq", "claude", ...) and is emitted even when gen_ai.provider.name is
# too: the spec's enum has no member for a stub or self-hosted provider, and the
# join back to an LLMConfiguration row must not depend on enum membership.
LLM_PROVIDER_CONFIGURED = "outreach.llm.provider"
LLM_ATTEMPT = "outreach.llm.attempt"
LLM_RETRY_AFTER_S = "outreach.llm.retry_after_s"

# Content references. The trace records WHICH record was read and written and a
# digest of each, never the text -- prompts embed the lead's HubSpot notes and
# completions are the outreach copy itself.
INPUT_REF = "outreach.input.ref"
INPUT_SHA256 = "outreach.input.sha256"
OUTPUT_REF = "outreach.output.ref"
OUTPUT_SHA256 = "outreach.output.sha256"

# outreach.verify.outcome allowed values. Six states, and the distinction
# between the last two is the one that matters on a dashboard: "skipped" is a
# decision (no automated pattern matched), "not_generated" is a failure (the
# provider call never produced text).
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
