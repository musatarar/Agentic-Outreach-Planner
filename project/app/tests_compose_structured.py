"""Component artifact: structured (MUS-47, component 6).

``tool_choice`` on the LLM abstraction — the mechanism that turns the copy
generator's "here are some tools, do as you like" into the read stage's "call
exactly this one thing". Structured output, not agency.

Planted red by the skeleton PR: every test carries ``@unittest.expectedFailure``
because ``agenerate_chat`` has no ``tool_choice`` parameter on this branch, so
each dies on a ``TypeError`` at the call. The component PR strips the markers and
takes this module to zero, leaving sibling artifacts' marker counts untouched
(docs/contracts/run-composer.md).

**Depends on MUS-66 landing on ``master`` first.** Blank arguments,
``finish_reason`` handling and dropped entries all sit directly underneath this
mechanism — forcing a call is how you get a model to emit one — and blankness has
a *different wire shape per adapter*: an empty ``input`` mapping on Anthropic, an
empty ``arguments`` string on the OpenAI-compatible path. Both are pinned here,
one per adapter class.

The load-bearing test is neither of the forcing ones. It is
``test_passing_tool_choice_none_sends_todays_body_byte_for_byte``, present on
*both* adapters: the MUS-29 agent loop passes no ``tool_choice`` at all, and
widening a shared adapter signature is the classic way to move a request body by
accident and find out in production. Bodies are compared as serialized JSON, key
order included, and each is first checked to be a real body — two empty dicts
compare equal just as happily and prove nothing.

Transport is mocked at the seams the existing adapter tests use:
``anthropic.AsyncAnthropic`` for Claude (``tests_llm_async.ClaudeAsyncTests``,
``tests_agent_loop_llm_tools.ClaudeToolCallTests``) and
``openai_compatible.httpx.AsyncClient`` for the OpenAI-compatible path
(``tests_llm_async.OpenAICompatibleAsyncTests``). ``SimpleTestCase`` throughout:
nothing here may touch a database, a network or a real key, and ``SimpleTestCase``
enforces the first of those rather than trusting it.
"""

import asyncio
import inspect
import json
import os
import unittest
from unittest import mock

from django.test import SimpleTestCase

from project.app.services.llm import claude as claude_mod
from project.app.services.llm import openai_compatible as oa_mod
from project.app.services.llm import stub as stub_mod
from project.app.services.llm.base import FINISH_TOOL_CALLS, LLMClient
from project.app.services.llm.chat_types import Message, ToolCallRequest, ToolSpec
from project.app.services.llm.errors import LLMError
from project.app.services.llm.groq import GroqClient

# One of MUS-29's four read-only agent tools. Present as the tool that is *not*
# being forced, so "a name not among the offered tools" is a realistic mistake
# (offering the agent's toolset and forcing the composer's) rather than a typo.
HISTORY_TOOL = ToolSpec(
    name="get_lead_history",
    description="d",
    parameters={"type": "object", "properties": {}},
)

# The composer's read tool, declared locally rather than imported from
# ``services/compose/read.py``: that module belongs to component 7 and this
# component must not depend on it. Only the *name* is contractual here — the
# adapters neither read nor validate the schema. Note the absence of
# ``lead_id``: the acting lead is bound server-side by the read loop, never
# taken from model output (docs/contracts/run-composer.md, "read").
SUGGESTION_TOOL = ToolSpec(
    name="emit_suggestion",
    description="Emit one advisory suggestion about the lead currently under review.",
    parameters={
        "type": "object",
        "properties": {
            "suggestion": {"type": "string", "enum": ["raise", "lower", "action_change", "none"]},
            "proposed_priority": {"type": "integer"},
            "proposed_action": {"type": "string"},
            "rationale": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["suggestion"],
    },
)

# The contract's five-key suggestion payload, used verbatim so the adapters'
# "hand the arguments back unchanged" claim is tested against what component 7
# will actually receive — nesting included, which is what a JSON round-trip on
# the OpenAI-compatible path can lose. The adapters neither validate nor reshape
# it; ``validate_suggestion`` owns that.
SUGGESTION_ARGUMENTS = {
    "suggestion": "raise",
    "proposed_priority": 1,
    "proposed_action": "",
    "rationale": "Renewal in three weeks and no contact since April.",
    "evidence": [{"source": "note", "quote": "renewal is three weeks out"}],
}

OPENING = [Message(role="user", content="- Agency: SYNTH-001")]

# The two ways a forced name can be absent from the offer. The empty-``tools``
# case is the one a membership check written as a truthiness test on ``tools``
# gets right only by accident.
ABSENT_NAME_CASES = (("others offered", (HISTORY_TOOL,)), ("none offered", ()))


class _Obj:
    """Attribute bag standing in for an SDK response object.

    Same helper as ``tests_agent_loop_llm_tools``: a ``mock.Mock`` invents any
    attribute you read, so a typo in the adapter's field name would still find
    something truthy and the assertion would pass on nothing.
    """

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _usage():
    return _Obj(
        input_tokens=900,
        output_tokens=40,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )


def _claude_suggestion_response(arguments=None):
    """A Claude turn that called ``emit_suggestion`` and said nothing else.

    ``input`` is a *mapping* on this wire format — Anthropic parses the arguments
    for us — which is why "blank" means ``{}`` here and ``""`` on the
    OpenAI-compatible path.
    """
    return _Obj(
        content=[
            _Obj(
                type="tool_use",
                id="toolu_01",
                name="emit_suggestion",
                input=dict(SUGGESTION_ARGUMENTS) if arguments is None else arguments,
            )
        ],
        stop_reason="tool_use",
        model="claude-sonnet-4-6",
        usage=_usage(),
    )


def _oa_suggestion_body(arguments=None):
    """The OpenAI-compatible counterpart. ``function.arguments`` is a JSON
    *string* on this wire format, which is why a blank one is a shape worth
    pinning."""
    if arguments is None:
        arguments = json.dumps(SUGGESTION_ARGUMENTS)
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "emit_suggestion", "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "model": "m",
        "usage": {"prompt_tokens": 900, "completion_tokens": 40},
    }


class ToolChoiceSignatureTests(SimpleTestCase):
    """The parameter's shape, before anything about its effect.

    Keyword-only and defaulting to ``None`` is what makes this additive: every
    MUS-29 call site passes messages and ``tools=`` and must keep compiling
    untouched, and no positional caller can drift into forcing a tool by
    miscounting arguments. Asserted on the base class *and* all three overrides,
    because an adapter that silently kept the narrower signature would fail at
    call time in production rather than at import time here.
    """

    @unittest.expectedFailure
    def test_tool_choice_is_a_keyword_only_parameter_defaulting_to_none(self):
        owners = (
            LLMClient,
            claude_mod.ClaudeClient,
            oa_mod.OpenAICompatibleClient,
            stub_mod.StubClient,
        )
        for owner in owners:
            with self.subTest(owner=owner.__name__):
                parameters = inspect.signature(owner.agenerate_chat).parameters
                self.assertIn("tool_choice", parameters)
                self.assertIs(parameters["tool_choice"].kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIsNone(parameters["tool_choice"].default)


class ClaudeToolChoiceTests(SimpleTestCase):
    def _patch_sdk(self, response):
        """The async SDK client, mocked where ``tests_llm_async`` mocks it.

        The kwargs handed to ``messages.create`` *are* the request body the SDK
        would serialize, so capturing them is capturing the wire payload.
        ``auth_token`` and ``credentials`` are pinned to ``None`` rather than
        left to the Mock's attribute invention — ``_check_credentials`` reads all
        three and would otherwise pass on an auto-attribute.
        """
        patcher = mock.patch.object(claude_mod.anthropic, "AsyncAnthropic")
        mock_cls = patcher.start()
        self.addCleanup(patcher.stop)
        client = mock_cls.return_value
        client.api_key = "test-key"
        client.auth_token = None
        client.credentials = None
        client.messages.create = mock.AsyncMock(return_value=response)
        return client

    @unittest.expectedFailure
    def test_passing_tool_choice_none_sends_todays_body_byte_for_byte(self):
        """``tool_choice=None`` is today's behavior, and "today's" is literal.

        The agent loop never passes the parameter; a run-composer change that
        added an empty ``tool_choice`` key, or reordered the body while wiring
        one in, would alter every MUS-29 request for no reason. Serialized
        without ``sort_keys``, so key *order* is compared too.
        """
        client = self._patch_sdk(_claude_suggestion_response())
        adapter = claude_mod.ClaudeClient(api_key="k")

        async def _both_bodies():
            # First call is the MUS-29 shape exactly: no tool_choice argument.
            await adapter.agenerate_chat(OPENING, tools=(SUGGESTION_TOOL,))
            omitted = json.dumps(client.messages.create.await_args.kwargs)
            await adapter.agenerate_chat(OPENING, tools=(SUGGESTION_TOOL,), tool_choice=None)
            return omitted, json.dumps(client.messages.create.await_args.kwargs)

        omitted, explicit_none = asyncio.run(_both_bodies())
        # What was captured is a real request body. Two empty dicts would satisfy
        # the equality below just as well and pin nothing.
        captured = json.loads(omitted)
        self.assertEqual(captured["model"], adapter.model)
        self.assertEqual([t["name"] for t in captured["tools"]], ["emit_suggestion"])
        self.assertEqual(explicit_none, omitted)
        # Not merely equal to each other — the key is absent from the wire.
        self.assertNotIn("tool_choice", omitted)

    @unittest.expectedFailure
    def test_a_forced_name_becomes_anthropics_tool_choice_block(self):
        """Anthropic spells a forced tool ``{"type": "tool", "name": n}``."""
        client = self._patch_sdk(_claude_suggestion_response())
        adapter = claude_mod.ClaudeClient(api_key="k")
        asyncio.run(
            adapter.agenerate_chat(
                OPENING,
                tools=(HISTORY_TOOL, SUGGESTION_TOOL),
                tool_choice="emit_suggestion",
            )
        )
        body = client.messages.create.await_args.kwargs
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "emit_suggestion"})
        # Forcing adds one key and moves nothing else: every offered tool is
        # still declared (the provider needs the forced tool's schema, and
        # dropping the others would be a silent second behavior change), and the
        # conversation is untouched.
        self.assertEqual(
            [t["name"] for t in body["tools"]], ["get_lead_history", "emit_suggestion"]
        )
        self.assertEqual(body["messages"], [{"role": "user", "content": OPENING[0].content}])

    @unittest.expectedFailure
    def test_a_name_that_is_not_on_offer_raises_before_the_request(self):
        """Forcing a name the model was never shown is a programming error on our
        side, and the provider would answer it with a 400 we pay for. It is caught
        before the request, and it is a plain ``ValueError`` rather than anything
        in the LLM taxonomy — an ``LLMError`` carries a ``retryable`` flag and
        would invite ``llm/retry.py`` to spend the whole budget re-sending a
        request that cannot ever succeed. Both absences are checked, including the
        empty-``tools`` one.
        """
        client = self._patch_sdk(_claude_suggestion_response())
        adapter = claude_mod.ClaudeClient(api_key="k")
        for label, tools in ABSENT_NAME_CASES:
            with self.subTest(offered=label):
                with self.assertRaises(ValueError) as caught:
                    asyncio.run(
                        adapter.agenerate_chat(OPENING, tools=tools, tool_choice="emit_suggestion")
                    )
                self.assertIn("emit_suggestion", str(caught.exception))
                self.assertNotIsInstance(caught.exception, LLMError)
                client.messages.create.assert_not_awaited()
        # Positive control: the same mock does record a legal call. Without it,
        # assert_not_awaited() would pass by construction if this patch ever
        # missed its target, and the test could not fail.
        asyncio.run(
            adapter.agenerate_chat(OPENING, tools=(SUGGESTION_TOOL,), tool_choice="emit_suggestion")
        )
        client.messages.create.assert_awaited_once()

    @unittest.expectedFailure
    def test_a_forced_call_comes_back_as_exactly_one_tool_call_request(self):
        """What the read stage consumes: one call, not a list to disambiguate.

        ``aread_all`` binds the acting lead to a single ``RunLead`` row, so a
        result carrying two calls would have no defined meaning. Forcing is what
        makes "exactly one" a property of the request rather than something the
        caller has to defend against.
        """
        self._patch_sdk(_claude_suggestion_response())
        adapter = claude_mod.ClaudeClient(api_key="k")
        result = asyncio.run(
            adapter.agenerate_chat(OPENING, tools=(SUGGESTION_TOOL,), tool_choice="emit_suggestion")
        )
        self.assertEqual(len(result.tool_calls), 1)
        call = result.tool_calls[0]
        self.assertIsInstance(call, ToolCallRequest)
        self.assertEqual(call.name, "emit_suggestion")
        self.assertEqual(call.id, "toolu_01")
        self.assertEqual(dict(call.arguments), SUGGESTION_ARGUMENTS)
        self.assertEqual(result.finish_reason, FINISH_TOOL_CALLS)
        self.assertEqual(result.text, "")  # structured output carries no prose

    @unittest.expectedFailure
    def test_blank_arguments_on_a_forced_call_are_read_as_an_empty_mapping(self):
        """MUS-66's shape in Anthropic's spelling: ``input`` present but empty.

        A forced call is exactly what produces one — the provider compels a call
        the model had nothing to put in — and the failure mode is not a quiet
        ``{}``. An adapter that treated an empty mapping as unreadable would drop
        the block, and ``stop_reason: tool_use`` with no readable block raises
        ``LLMEmptyCompletionError``: a malformed-provider report, non-retryable,
        for a lead whose read merely came back empty. The adapter's job ends at
        delivering ``{}``; ``validate_suggestion`` discards it and counts it into
        ``PlannerRun.discarded_suggestions``.
        """
        self._patch_sdk(_claude_suggestion_response(arguments={}))
        result = asyncio.run(
            claude_mod.ClaudeClient(api_key="k").agenerate_chat(
                OPENING, tools=(SUGGESTION_TOOL,), tool_choice="emit_suggestion"
            )
        )
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "emit_suggestion")
        self.assertEqual(dict(result.tool_calls[0].arguments), {})
        self.assertEqual(result.finish_reason, FINISH_TOOL_CALLS)


class OpenAICompatibleToolChoiceTests(SimpleTestCase):
    """Exercised through ``GroqClient``, as the existing adapter tests do — the
    shared class is abstract about ``base_url``/``api_key_env``."""

    def _patch_client(self, body):
        patcher = mock.patch("project.app.services.llm.openai_compatible.httpx.AsyncClient")
        mock_cls = patcher.start()
        self.addCleanup(patcher.stop)
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = body
        client = mock_cls.return_value
        client.post = mock.AsyncMock(return_value=response)
        client.aclose = mock.AsyncMock()
        return client

    @unittest.expectedFailure
    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_passing_tool_choice_none_sends_todays_body_byte_for_byte(self):
        """The Claude pin's twin, and the one that matters more: here the body is
        a dict we build ourselves, so an extra key or a reordering is entirely
        within our reach to introduce."""
        client = self._patch_client(_oa_suggestion_body())
        adapter = GroqClient(model="m")

        async def _both_bodies():
            await adapter.agenerate_chat(OPENING, tools=(SUGGESTION_TOOL,))
            omitted = json.dumps(client.post.await_args.kwargs["json"])
            await adapter.agenerate_chat(OPENING, tools=(SUGGESTION_TOOL,), tool_choice=None)
            return omitted, json.dumps(client.post.await_args.kwargs["json"])

        omitted, explicit_none = asyncio.run(_both_bodies())
        captured = json.loads(omitted)
        self.assertEqual(captured["model"], "m")
        self.assertEqual([t["function"]["name"] for t in captured["tools"]], ["emit_suggestion"])
        self.assertEqual(explicit_none, omitted)
        self.assertNotIn("tool_choice", omitted)

    @unittest.expectedFailure
    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_a_forced_name_becomes_the_function_envelope(self):
        """This wire format nests the name one level deeper than Anthropic's,
        under ``function``. Sending Anthropic's flat shape here is a 400."""
        client = self._patch_client(_oa_suggestion_body())
        asyncio.run(
            GroqClient(model="m").agenerate_chat(
                OPENING,
                tools=(HISTORY_TOOL, SUGGESTION_TOOL),
                tool_choice="emit_suggestion",
            )
        )
        body = client.post.await_args.kwargs["json"]
        self.assertEqual(
            body["tool_choice"], {"type": "function", "function": {"name": "emit_suggestion"}}
        )
        self.assertEqual(
            [t["function"]["name"] for t in body["tools"]],
            ["get_lead_history", "emit_suggestion"],
        )
        self.assertEqual(body["messages"], [{"role": "user", "content": OPENING[0].content}])

    @unittest.expectedFailure
    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_a_name_that_is_not_on_offer_raises_before_the_request(self):
        """Same rule as Claude's, checked once in the shared layer so the two
        adapters cannot disagree about what a valid forced name is — same two
        absences, same plain ``ValueError``, same naming of the offending tool."""
        client = self._patch_client(_oa_suggestion_body())
        adapter = GroqClient(model="m")
        for label, tools in ABSENT_NAME_CASES:
            with self.subTest(offered=label):
                with self.assertRaises(ValueError) as caught:
                    asyncio.run(
                        adapter.agenerate_chat(OPENING, tools=tools, tool_choice="emit_suggestion")
                    )
                self.assertIn("emit_suggestion", str(caught.exception))
                self.assertNotIsInstance(caught.exception, LLMError)
                client.post.assert_not_awaited()
        # Positive control, as on the Claude path: proof the patched seam is the
        # one the adapter posts through, so the no-call assertions can fail.
        asyncio.run(
            adapter.agenerate_chat(OPENING, tools=(SUGGESTION_TOOL,), tool_choice="emit_suggestion")
        )
        client.post.assert_awaited_once()

    @unittest.expectedFailure
    @mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
    def test_blank_arguments_on_a_forced_call_are_read_as_an_empty_mapping(self):
        """MUS-66's shape in this wire format's spelling: ``arguments`` is a JSON
        string, and a forced call is what produces a blank one. Today
        ``json.loads("")`` raises and the adapter turns that into a non-retryable
        ``LLMMalformedResponseError`` — reporting a malformed provider response
        for a lead whose read merely came back empty. An empty argument object is
        a *content* problem: the adapter delivers ``{}`` and
        ``validate_suggestion`` discards it.
        """
        self._patch_client(_oa_suggestion_body(arguments=""))
        result = asyncio.run(
            GroqClient(model="m").agenerate_chat(
                OPENING, tools=(SUGGESTION_TOOL,), tool_choice="emit_suggestion"
            )
        )
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "emit_suggestion")
        self.assertEqual(dict(result.tool_calls[0].arguments), {})
        self.assertEqual(result.finish_reason, FINISH_TOOL_CALLS)


class StubToolChoiceTests(SimpleTestCase):
    """The offline provider has to honour forcing too.

    ``evals/bench_planner.py`` is the only thing that can build one, and a stub
    that ignored ``tool_choice`` would answer every forced request with
    ``tools[0]`` — making a benchmark of the read stage measure a tool the run
    never asked for, and hiding exactly the bug this parameter exists to avoid.
    """

    def _client(self):
        with mock.patch.dict("os.environ", {stub_mod.ALLOW_ENV_VAR: "1"}):
            return stub_mod.StubClient(latency_mean_s=0.0, latency_stddev_s=0.0, seed=7)

    @unittest.expectedFailure
    def test_the_stub_calls_the_forced_tool_rather_than_the_first_one_offered(self):
        """``emit_suggestion`` is deliberately *not* first in the sequence: the
        current script takes ``tools[0]``, so forcing the first entry would pass
        without the stub having read ``tool_choice`` at all."""
        result = asyncio.run(
            self._client().agenerate_chat(
                OPENING,
                tools=(HISTORY_TOOL, SUGGESTION_TOOL),
                tool_choice="emit_suggestion",
            )
        )
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "emit_suggestion")
        self.assertNotEqual(result.tool_calls[0].name, HISTORY_TOOL.name)
        self.assertEqual(result.finish_reason, FINISH_TOOL_CALLS)

    @unittest.expectedFailure
    def test_the_stub_refuses_a_name_it_was_not_offered(self):
        """The validation belongs above the adapters, not inside each one: a stub
        that accepted a name Claude rejects would let a benchmark run green over a
        call the real provider 400s. Same two absences, same named tool."""
        for label, tools in ABSENT_NAME_CASES:
            with self.subTest(offered=label):
                with self.assertRaises(ValueError) as caught:
                    asyncio.run(
                        self._client().agenerate_chat(
                            OPENING, tools=tools, tool_choice="emit_suggestion"
                        )
                    )
                self.assertIn("emit_suggestion", str(caught.exception))

    @unittest.expectedFailure
    def test_tool_choice_none_replays_todays_stub_script_exactly(self):
        """The stub's half of the no-change guarantee. Its script is stateless by
        design (clients are ``lru_cache``d singletons), so replaying the same
        opening conversation is a legitimate comparison rather than a second step
        of some sequence."""
        client = self._client()
        tools = (HISTORY_TOOL, SUGGESTION_TOOL)
        baseline = asyncio.run(client.agenerate_chat(OPENING, tools=tools))
        explicit_none = asyncio.run(client.agenerate_chat(OPENING, tools=tools, tool_choice=None))
        self.assertEqual(explicit_none.tool_calls[0].name, baseline.tool_calls[0].name)
        # And that unchanged behavior is specifically "ask for tools[0]".
        self.assertEqual(explicit_none.tool_calls[0].name, HISTORY_TOOL.name)
        self.assertEqual(explicit_none.finish_reason, baseline.finish_reason)
