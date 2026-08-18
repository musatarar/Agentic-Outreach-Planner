"""The bounded tool-calling loop for one lead (MUS-29).

Budgets (steps, tool calls, wall clock) come from ``PlannerRuntime``; every
provider response and its consequent step records are persisted through the
checkpoint before the loop continues, which is what bounds crash re-billing at
one call per in-flight lead.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from project.app.services.agent.state import (
    KIND_FINAL,
    KIND_LLM_CALL,
    KIND_TOOL_RESULT,
    PAYLOAD_MODEL,
    PAYLOAD_PROVIDER,
    PAYLOAD_REQUEST_SHA256,
    PAYLOAD_RESULT_SHA256,
    STATUS_DONE,
    STATUS_DRAFTING,
    STATUS_EXHAUSTED,
    STATUS_FAILED,
    AgentClaimLost,
    Checkpoint,
    StepRecord,
    fold_messages,
)
from project.app.services.agent.tools import (
    TOOL_SPECS,
    ToolContext,
    UnknownTool,
    execute_tool,
)
from project.app.services.llm.base import FINISH_TOOL_CALLS, LLMClient, LLMResult
from project.app.services.llm.chat_types import Message, ToolCallRequest
from project.app.services.llm.errors import LLMError
from project.app.services.llm.retry import acall_with_retry
from project.app.services.llm.runtime import PlannerRuntime


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    """What the loop hands back to phase 3.

    Carries no ``action_type``, ``priority`` or ``needs_human``: the rules
    engine keeps sole authority over classification. ``attempts`` (provider
    attempts summed across every step) and ``elapsed_s`` feed phase 4's failure
    sentence and are meaningful only on a failure.
    """

    draft_text: str = ""
    error: Exception | None = None
    steps_used: int = 0
    tool_calls_used: int = 0
    attempts: int = 0
    elapsed_s: float = 0.0


def _rendered_request(messages: Sequence[Message]) -> str:
    """One canonical string per request, for the step row's ``request_sha256``."""
    return json.dumps(
        [
            {
                "role": message.role,
                "content": message.content,
                "tool_call_id": message.tool_call_id,
                "tool_calls": [
                    {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
                    for call in message.tool_calls
                ],
            }
            for message in messages
        ],
        sort_keys=True,
        default=str,
    )


def _llm_call_payload(result: LLMResult, request_sha256: str | None) -> dict[str, Any]:
    return {
        "text": result.text,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
            for call in result.tool_calls
        ],
        "provider": result.provider,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "raw_finish_reason": result.raw_finish_reason,
        "latency_s": result.latency_s,
        PAYLOAD_REQUEST_SHA256: request_sha256,
        # Popped by the checkpoint into a ProviderTrace row (MUS-72); the
        # payload's own "provider"/"model" above stay, for the reports page.
        PAYLOAD_PROVIDER: result.provider,
        PAYLOAD_MODEL: result.model,
    }


def _execute_one_tool(genai: Any, call: ToolCallRequest, context: ToolContext) -> str:
    """Run one tool inside its ``execute_tool`` span; hashes only, never payloads."""
    args_sha256 = genai.sha256_of(json.dumps(dict(call.arguments), sort_keys=True, default=str))
    with genai.tool_span(call.name, args_sha256=args_sha256) as set_result_sha256:
        rendered = execute_tool(call.name, call.arguments, context)
        set_result_sha256(genai.sha256_of(rendered))
    return rendered


async def run_agent_lead(
    *,
    prompt: str,
    lead_run_pk: int,
    prior_steps: Sequence[StepRecord],
    context: ToolContext,
    client: LLMClient,
    runtime: PlannerRuntime,
    checkpoint: Checkpoint,
) -> AgentOutcome:
    """Drive one lead's loop: claim, fold, call, execute tools, checkpoint.

    ``LLMError``, ``TimeoutError`` and ``UnknownTool`` become
    ``AgentOutcome(error=...)`` with the run checkpointed ``failed``;
    ``AgentClaimLost`` does the same but writes nothing, since another worker
    owns the run and its status must not be trampled.
    """
    steps = list(prior_steps)
    steps_used = sum(1 for step in steps if step.kind == KIND_LLM_CALL)
    tool_calls_used = sum(1 for step in steps if step.kind == KIND_TOOL_RESULT)

    # Counted here rather than derived from `steps_used`: a step that failed
    # every retry persists no step record and would otherwise report zero.
    attempts = 0
    started = time.monotonic()

    def outcome(*, draft_text: str = "", error: Exception | None = None) -> AgentOutcome:
        """One exit shape, so no return path can forget the accounting."""
        return AgentOutcome(
            draft_text=draft_text,
            error=error,
            steps_used=steps_used,
            tool_calls_used=tool_calls_used,
            attempts=attempts,
            elapsed_s=time.monotonic() - started,
        )

    # Resume replay: a persisted final is the answer — zero provider calls.
    if steps and steps[-1].kind == KIND_FINAL:
        return outcome(draft_text=str(steps[-1].payload.get("text") or ""))

    if not await checkpoint.claim(lead_run_pk):
        return outcome(error=AgentClaimLost(f"run {lead_run_pk}: another worker owns the claim"))

    # Function-local: outreach.py imports this module, so a top-level import
    # would be a cycle.
    from project.app.services.outreach import MAX_COPY_TOKENS
    from project.app.services.telemetry import genai

    # One AgentLeadRun claim epoch owns one gapless seq line; resume continues it.
    seq = steps[-1].seq if steps else 0

    # Shared by every attempt for this lead: one CLIENT span per HTTP attempt
    # (MUS-25), same shape as agenerate_copy.
    call_scope = genai.provider_call_scope(genai.ProviderCall.from_client(client, MAX_COPY_TOKENS))

    try:
        async with asyncio.timeout(runtime.agent_per_lead_s):
            while steps_used < runtime.agent_max_steps:
                force_final = (
                    steps_used == runtime.agent_max_steps - 1
                    or tool_calls_used >= runtime.agent_max_tool_calls
                )
                messages = fold_messages(prompt, steps, force_final=force_final)
                offered = () if force_final else TOOL_SPECS

                async def attempt(
                    messages: Sequence[Message] = messages,
                    offered: Sequence[Any] = offered,
                ) -> LLMResult:
                    nonlocal attempts
                    attempts += 1
                    return await client.agenerate_chat(
                        messages,
                        tools=offered,
                        max_tokens=MAX_COPY_TOKENS,
                        timeout=runtime.timeouts.request_s,
                    )

                result = await acall_with_retry(
                    attempt, policy=runtime.retry, attempt_scope=call_scope
                )
                steps_used += 1
                seq += 1
                records = [
                    StepRecord(
                        seq=seq,
                        kind=KIND_LLM_CALL,
                        payload=_llm_call_payload(
                            result, genai.sha256_of(_rendered_request(messages))
                        ),
                    )
                ]

                # A forced-final call offered no tools; any tool call the model
                # invents there has nothing to execute against.
                tool_calls = () if force_final else result.tool_calls
                if tool_calls:
                    for call in tool_calls:
                        rendered = _execute_one_tool(genai, call, context)
                        tool_calls_used += 1
                        seq += 1
                        records.append(
                            StepRecord(
                                seq=seq,
                                kind=KIND_TOOL_RESULT,
                                payload={
                                    "tool_call_id": call.id,
                                    "name": call.name,
                                    "result": rendered,
                                    PAYLOAD_RESULT_SHA256: genai.sha256_of(rendered),
                                },
                            )
                        )
                    await checkpoint.append(
                        lead_run_pk,
                        records,
                        status=STATUS_DRAFTING,
                        steps_used=steps_used,
                        tool_calls_used=tool_calls_used,
                    )
                    steps.extend(records)
                    continue

                # Text riding along with a tool-call turn is the model narrating
                # its way to a call, not the email; persisting it as the draft
                # is how function-call markers once reached a reviewer (MUS-66).
                # Falling through marks the run exhausted instead.
                if result.text and result.finish_reason != FINISH_TOOL_CALLS:
                    seq += 1
                    records.append(
                        StepRecord(
                            seq=seq,
                            kind=KIND_FINAL,
                            payload={
                                "text": result.text,
                                PAYLOAD_RESULT_SHA256: genai.sha256_of(result.text),
                            },
                        )
                    )
                    await checkpoint.append(
                        lead_run_pk,
                        records,
                        status=STATUS_DONE,
                        steps_used=steps_used,
                        tool_calls_used=tool_calls_used,
                    )
                    return outcome(draft_text=result.text)

                # No usable final: the budget ran out before a draft. Persist
                # the call and mark the run exhausted rather than inventing an
                # empty final step.
                await checkpoint.append(
                    lead_run_pk,
                    records,
                    status=STATUS_EXHAUSTED,
                    steps_used=steps_used,
                    tool_calls_used=tool_calls_used,
                )
                return outcome()

        # Entered with the step budget already spent (a resumed run that
        # crashed after its last allowed call) and no final on record.
        await checkpoint.append(
            lead_run_pk,
            (),
            status=STATUS_EXHAUSTED,
            steps_used=steps_used,
            tool_calls_used=tool_calls_used,
        )
        return outcome()
    except AgentClaimLost as exc:
        # Surfaced from an append: the run has a new owner. Write nothing.
        return outcome(error=exc)
    except (LLMError, TimeoutError, UnknownTool) as exc:
        try:
            await checkpoint.append(
                lead_run_pk,
                (),
                status=STATUS_FAILED,
                steps_used=steps_used,
                tool_calls_used=tool_calls_used,
            )
        except AgentClaimLost:
            # The claim moved while this worker was failing; the new owner's
            # status wins and the original error still describes this attempt.
            pass
        return outcome(error=exc)
