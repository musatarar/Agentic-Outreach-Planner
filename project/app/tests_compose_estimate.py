"""Component artifact: estimate (MUS-47 component 5).

"Nothing spends without a price shown first" is only worth saying if the price is
real, and a price is real when it moves. The number comes from the MUS-32
``LLMModel`` catalog row and follows an operator's edit to it, it is a ``Decimal``
carrying the four places the run's cost columns store, it labels itself an estimate,
and ``estimate_stage`` writes it onto the run so the estimate-vs-actual gap is
recoverable. ``record_actuals`` closes the loop: it sums what the providers actually
reported, prices it at the pair that actually ran, and refuses to render an
unobserved usage report as a confident zero.

Red skeleton: ``PlannerRun``/``RunLead`` and everything in
``services/compose/estimate`` land in later PRs, so those names are imported *inside*
the test bodies -- a module-level import of a symbol that does not exist yet takes the
whole file out of collection. The one green test is the anonymous 401 at the bottom.
"""

import datetime
import unittest
from decimal import Decimal
from urllib.parse import urlencode

from django.test import TestCase

from project.app.models import Lead, LLMModel, LLMProvider
from project.app.services.actions import NUDGE_USAGE
from project.app.services.dedupe import dedupe_key
from project.app.services.llm.base import LLMResult
from project.app.services.outreach import explain
from project.app.services.sanitize import MAX_NOTE_CHARS
from project.app.tests_auth_utils import AuthenticatedAPITestCase

PROVIDER_KEY = "claude"
PRIMARY_MODEL = "model-primary"
OTHER_MODEL = "model-other"
TODAY = datetime.date(2026, 3, 1)
ESTIMATE_LOGGER = "project.app.services.compose.estimate"

# Absurd prices, chosen on purpose. At a whole multiple of 100 USD per million
# tokens, ``tokens / 1_000_000 * price`` always lands exactly on four decimal
# places, so the equality assertions below pin the arithmetic and the catalog
# lookup instead of quietly pinning a rounding mode nobody has chosen yet.
PRIMARY_IN = Decimal("100.0000")
PRIMARY_OUT = Decimal("300.0000")
# Input and output are inverted relative to PRIMARY so a transposed pair of price
# columns cannot survive any test in this file.
OTHER_IN = Decimal("400.0000")
OTHER_OUT = Decimal("200.0000")

MILLION = Decimal(1_000_000)
QUANTUM = Decimal("0.0001")  # DecimalField(decimal_places=4) on PlannerRun


def _price(tokens_in, tokens_out, in_price, out_price):
    """The cost of a call, worked out by hand from a catalog row. Deliberately not a
    call into the module under test: an assertion computed by the code it is checking
    proves only that the code is self-consistent."""
    total = (Decimal(tokens_in) * in_price + Decimal(tokens_out) * out_price) / MILLION
    return total.quantize(QUANTUM)


def _seed_catalog():
    """Two priced rows behind one provider -- MUS-32's catalog, in miniature."""
    provider = LLMProvider.objects.create(
        key=PROVIDER_KEY,
        label="Anthropic Claude",
        api_key_url="https://console.anthropic.com/settings/keys",
        api_key_label="Anthropic API key",
        api_key_prefix="sk-ant-",
    )
    for model_id, label, in_price, out_price in (
        (PRIMARY_MODEL, "Primary", PRIMARY_IN, PRIMARY_OUT),
        (OTHER_MODEL, "Other", OTHER_IN, OTHER_OUT),
    ):
        LLMModel.objects.create(
            provider=provider,
            model_id=model_id,
            label=label,
            context_window=200_000,
            default_max_tokens=500,
            input_price_per_mtok_usd=in_price,
            output_price_per_mtok_usd=out_price,
        )


def _make_lead(suffix, *, notes="Short note."):
    """A lead in the shape ``ingest_data`` writes, complete enough for
    ``outreach.explain()`` to score. Every field that varies with ``suffix`` keeps the
    same character count, so leads seeded here are interchangeable to anything
    estimating from prompt length."""
    return Lead.objects.create(
        id=f"lead_est{suffix}",
        agency_name=f"Agency est{suffix}",
        contact_name=f"Contact est{suffix}",
        contact_email=f"lead_est{suffix}@example.com",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=5,
        estimated_book_size_usd=1_000_000,
        stage="active_trial",
        signed_up_date=datetime.date(2026, 1, 10),
        last_login_date=datetime.date(2026, 2, 1),
        last_contacted_date=datetime.date(2026, 1, 20),
        quotes_created=4,
        quotes_submitted=1,
        deals_closed=0,
        hubspot_notes=notes,
    )


def _make_run(*, pk=None):
    """An active, classified run. ``PlannerRun`` arrives with component 1, hence the
    deferred import. ``pk`` is explicit only where a test matches on digits in a log
    line and needs the run's own id out of the way."""
    from project.app.models import PlannerRun

    return PlannerRun.objects.create(
        pk=pk,
        status=PlannerRun.STATUS_CLASSIFIED,
        scope={},
        created_by="tester@example.com",
    )


def _add_run_lead(run, lead, *, selected=True, already_queued=False):
    """One classified row: the ``rules_*`` columns as classify wrote them, the
    ``effective_*`` columns still copies of them, a real ``dedupe_key``, and the real
    ``explain()`` envelope ``rule_trace`` stores -- there is no other trace schema."""
    from project.app.models import RunLead

    return RunLead.objects.create(
        run=run,
        lead=lead,
        rules_priority=2,
        rules_action=NUDGE_USAGE,
        rules_reason="Underusing the portal.",
        rule_trace=explain(lead, today=TODAY),
        dedupe_key=dedupe_key(lead.id, NUDGE_USAGE),
        effective_priority=2,
        effective_action=NUDGE_USAGE,
        effective_reason="Underusing the portal.",
        selected=selected,
        already_queued=already_queued,
    )


def _result(*, input_tokens=None, output_tokens=None, model=PRIMARY_MODEL):
    """One completed provider call. The token counts default to ``None`` exactly
    as :class:`LLMResult` does -- a result that never observed usage, which is not
    the same thing as one that observed zero."""
    return LLMResult(
        text="ok",
        provider=PROVIDER_KEY,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class EstimateStageTests(TestCase):
    """``estimate_stage`` on its own, away from HTTP. ``setUpTestData`` touches only
    models that exist on this branch: an exception in class or instance setup is an
    ERROR and is *not* absorbed by ``expectedFailure``, so nothing red is allowed above
    the test bodies."""

    @classmethod
    def setUpTestData(cls):
        _seed_catalog()
        cls.leads = [_make_lead(n) for n in (1, 2, 3)]

    def _classified_run(self, lead_count=1, **row_kwargs):
        run = _make_run()
        for lead in self.leads[:lead_count]:
            _add_run_lead(run, lead, **row_kwargs)
        return run

    @unittest.expectedFailure
    def test_usd_est_is_the_catalog_price_applied_to_the_estimated_tokens(self):
        """The estimate has to be the catalog row's arithmetic, not a private table of
        prices this module keeps for itself. Only hand-worked multiplication tells those
        two apart; a second, differently priced model shows the price travelling with
        the pair."""
        from project.app.services.compose import estimate

        run = self._classified_run(lead_count=3)

        est = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)
        self.assertEqual(
            est.usd_est,
            _price(est.tokens_in_est, est.tokens_out_est, PRIMARY_IN, PRIMARY_OUT),
        )

        other = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=OTHER_MODEL)
        self.assertEqual(other.tokens_in_est, est.tokens_in_est)  # same run, same prompts
        self.assertEqual(other.tokens_out_est, est.tokens_out_est)
        self.assertEqual(
            other.usd_est,
            _price(other.tokens_in_est, other.tokens_out_est, OTHER_IN, OTHER_OUT),
        )
        self.assertNotEqual(other.usd_est, est.usd_est)  # only the price differed

    @unittest.expectedFailure
    def test_the_estimate_moves_when_the_catalog_price_moves(self):
        """The half of that claim a hardcoded constant cannot fake: edit the row, ask
        again, get a different number. The free case is sharpest -- a module carrying its
        own prices would still quote a charge for a model the catalog says is free."""
        from project.app.services.compose import estimate

        run = self._classified_run()
        before = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)

        row = LLMModel.objects.get(provider_id=PROVIDER_KEY, model_id=PRIMARY_MODEL)
        row.input_price_per_mtok_usd = PRIMARY_IN * 2
        row.output_price_per_mtok_usd = PRIMARY_OUT * 2
        row.save(update_fields=["input_price_per_mtok_usd", "output_price_per_mtok_usd"])

        doubled = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)
        self.assertEqual(doubled.tokens_in_est, before.tokens_in_est)  # nothing but price moved
        self.assertEqual(doubled.usd_est, before.usd_est * 2)

        row.input_price_per_mtok_usd = Decimal("0.0000")
        row.output_price_per_mtok_usd = Decimal("0.0000")
        row.save(update_fields=["input_price_per_mtok_usd", "output_price_per_mtok_usd"])

        free = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)
        self.assertEqual(free.usd_est, Decimal("0.0000"))

    @unittest.expectedFailure
    def test_usd_est_is_a_decimal_quantized_to_the_column_it_will_be_stored_in(self):
        """Float money is a bug waiting for a rounding complaint. The run's cost columns
        are ``DecimalField(decimal_places=4)``, so a value not already carrying four
        places changes shape on the way into the database -- and the estimate a reviewer
        saw would not be the one the row records."""
        from project.app.services.compose import estimate

        run = self._classified_run(lead_count=3)
        est = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)

        self.assertIsInstance(est.usd_est, Decimal)
        # Decimal("0.8") == Decimal("0.8000"), so equality is blind to the
        # quantization; the exponent is the only thing that actually pins it.
        self.assertEqual(est.usd_est.as_tuple().exponent, -4)
        self.assertIsInstance(est.tokens_in_est, int)
        self.assertIsInstance(est.tokens_out_est, int)

    @unittest.expectedFailure
    def test_the_payload_labels_itself_an_estimate_and_echoes_its_inputs(self):
        """``is_estimate`` is a claim about provenance: modelled, not measured. The UI
        renders it with a tilde and the run summary sets it against
        ``*_cost_actual_usd``; both need the stage/provider/model it was computed for to
        travel with it rather than be remembered by the caller."""
        from project.app.services.compose import estimate

        run = self._classified_run(lead_count=3)

        for stage in (estimate.STAGE_READ, estimate.STAGE_GENERATE):
            with self.subTest(stage=stage):
                est = estimate.estimate_stage(
                    run, stage, provider=PROVIDER_KEY, model=PRIMARY_MODEL
                )
                self.assertIsInstance(est, estimate.Estimate)
                self.assertIs(est.is_estimate, True)
                self.assertEqual(est.stage, stage)
                self.assertEqual(est.provider, PROVIDER_KEY)
                self.assertEqual(est.model, PRIMARY_MODEL)
                self.assertEqual(est.lead_count, 3)

    @unittest.expectedFailure
    def test_tokens_out_est_is_lead_count_times_the_stage_constant(self):
        """Output length is the one thing an estimator can pin without seeing a prompt:
        one suggestion per lead at ``READ_OUTPUT_TOKENS``, one email per lead at
        ``GENERATE_OUTPUT_TOKENS``. Both stages see the same three selected rows, so the
        only difference left is the constant -- which is what makes a mix-up visible."""
        from project.app.services.compose import estimate

        run = self._classified_run(lead_count=3, selected=True, already_queued=False)

        read = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)
        gen = estimate.estimate_stage(run, "generate", provider=PROVIDER_KEY, model=PRIMARY_MODEL)

        self.assertEqual(read.lead_count, 3)
        self.assertEqual(gen.lead_count, 3)
        self.assertEqual(read.tokens_out_est, 3 * estimate.READ_OUTPUT_TOKENS)
        self.assertEqual(gen.tokens_out_est, 3 * estimate.GENERATE_OUTPUT_TOKENS)
        self.assertNotEqual(read.tokens_out_est, gen.tokens_out_est)
        self.assertNotEqual(read.usd_est, gen.usd_est)

    @unittest.expectedFailure
    def test_generate_prices_only_the_rows_it_would_actually_send(self):
        """``generate_for_selection`` generates for ``selected=True,
        already_queued=False`` rows only, so an estimate counting the whole run quotes
        for work the generator will not do -- and the reviewer reads the gap afterwards
        as the model coming in under budget. Read has no selection to respect: it runs
        before anyone has picked anything."""
        from project.app.services.compose import estimate

        run = _make_run()
        _add_run_lead(run, self.leads[0], selected=True, already_queued=False)
        _add_run_lead(run, self.leads[1], selected=True, already_queued=True)  # open action exists
        _add_run_lead(run, self.leads[2], selected=False, already_queued=False)  # not picked

        gen = estimate.estimate_stage(run, "generate", provider=PROVIDER_KEY, model=PRIMARY_MODEL)
        self.assertEqual(gen.lead_count, 1)
        self.assertEqual(gen.tokens_out_est, estimate.GENERATE_OUTPUT_TOKENS)

        read = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)
        self.assertEqual(read.lead_count, 3)
        self.assertEqual(read.tokens_out_est, 3 * estimate.READ_OUTPUT_TOKENS)
        # This 3-vs-1 asymmetry is why ``formatEstimate`` is stage-aware on the FE:
        # ``lead_count`` means "rows in the run" for a read and "rows you picked" for a
        # generate, so one wording for both ("3 selected") would credit the operator
        # with a choice they never made.

    @unittest.expectedFailure
    def test_tokens_in_est_scales_with_lead_count(self):
        """One prompt per lead, so the input side has to grow with the run. Growing a
        single run rather than comparing three is not stylistic -- ``pr_one_active_run``
        admits exactly one active run -- and it isolates lead count as the only mover."""
        from project.app.services.compose import estimate

        run = _make_run()
        counts = []
        for lead in self.leads:
            _add_run_lead(run, lead)
            est = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)
            counts.append(est.tokens_in_est)
        one, two, three = counts

        self.assertLess(one, two)
        self.assertLess(two, three)
        # Interchangeable leads must cost interchangeable tokens. A one-token wobble is
        # where the floor division sits, not a different model of what a lead costs.
        self.assertAlmostEqual(three - two, two - one, delta=1)

    @unittest.expectedFailure
    def test_tokens_in_est_counts_prompt_characters_at_chars_per_token(self):
        """The whole model of input cost is "prompt characters over ``CHARS_PER_TOKEN``",
        and the read prompt's variable part is the lead's notes. Padding by an exact
        multiple of ``CHARS_PER_TOKEN`` keeps the delta exact whatever fixed template the
        prompt adds and wherever the floor division sits (``floor(x + k) == floor(x) + k``
        for integer k), so this pins the divisor without pretending to know the base."""
        from project.app.services.compose import estimate

        lead = self.leads[0]
        run = _make_run()
        _add_run_lead(run, lead)
        before = estimate.estimate_stage(
            run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL
        ).tokens_in_est

        padding = "n" * (100 * estimate.CHARS_PER_TOKEN)
        # Stay well inside the sanitizer's note cap: past MAX_NOTE_CHARS the text
        # is truncated, and this would measure the cap instead of the ratio.
        self.assertLess(len(lead.hubspot_notes) + len(padding), MAX_NOTE_CHARS)
        Lead.objects.filter(pk=lead.pk).update(hubspot_notes=lead.hubspot_notes + padding)

        after = estimate.estimate_stage(
            run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL
        ).tokens_in_est
        self.assertEqual(after - before, len(padding) // estimate.CHARS_PER_TOKEN)

    @unittest.expectedFailure
    def test_an_unknown_provider_model_pair_raises_does_not_exist(self):
        """There is no fallback price and there must not be one. A pair with no catalog
        row cannot be quoted, and a default invented here reaches the reviewer wearing
        exactly as much confidence as a real number."""
        from project.app.services.compose import estimate

        run = self._classified_run()

        with self.assertRaises(LLMModel.DoesNotExist):
            estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model="not-in-catalog")
        with self.assertRaises(LLMModel.DoesNotExist):
            estimate.estimate_stage(run, "read", provider="not-a-provider", model=PRIMARY_MODEL)

    @unittest.expectedFailure
    def test_an_unknown_stage_raises_rather_than_guessing_a_constant(self):
        """Only two stages spend money. A third name is a caller bug, and silently
        reaching for one of the two output constants prices the wrong stage rather than
        refusing. ``ValueError`` matches ``ScopeError`` at the feature's other validated
        boundary."""
        from project.app.services.compose import estimate

        run = self._classified_run()

        for stage in ("classify", "", "READ"):
            with self.subTest(stage=stage):
                self.assertNotIn(stage, estimate.STAGES)
                with self.assertRaises(ValueError):
                    estimate.estimate_stage(run, stage, provider=PROVIDER_KEY, model=PRIMARY_MODEL)

    @unittest.expectedFailure
    def test_estimate_stage_persists_the_estimate_it_returns(self):
        """Nothing spends without a price shown first only has a mechanical form if the
        price shown is also the price recorded: the quote lands on the stage's own
        ``*_cost_estimate_usd`` column, and an estimate nobody stored cannot be compared
        to anything later. Estimating still is not spending -- the ``*_actual_usd`` and
        provider/model columns record what ran, and nothing has run."""
        from project.app.services.compose import estimate

        run = self._classified_run(lead_count=3)

        read = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)
        self.assertGreater(read.usd_est, Decimal("0.0000"))  # a real quote, not a zero
        run.refresh_from_db()
        self.assertEqual(run.read_cost_estimate_usd, read.usd_est)
        self.assertIsNone(run.generate_cost_estimate_usd)  # one stage, one column
        self.assertIsNone(run.read_cost_actual_usd)
        self.assertEqual(run.read_provider, "")
        self.assertEqual(run.read_model, "")

        gen = estimate.estimate_stage(run, "generate", provider=PROVIDER_KEY, model=OTHER_MODEL)
        run.refresh_from_db()
        self.assertEqual(run.generate_cost_estimate_usd, gen.usd_est)
        self.assertEqual(run.read_cost_estimate_usd, read.usd_est)  # the other stage stood still

        # Re-quoting overwrites: the column holds the price the reviewer last saw,
        # which is the one the actual will be measured against.
        requote = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=OTHER_MODEL)
        self.assertNotEqual(requote.usd_est, read.usd_est)
        run.refresh_from_db()
        self.assertEqual(run.read_cost_estimate_usd, requote.usd_est)


class RecordActualsTests(TestCase):
    """``record_actuals`` -- the other side of every estimate this module makes."""

    @classmethod
    def setUpTestData(cls):
        _seed_catalog()

    @unittest.expectedFailure
    def test_record_actuals_sums_reported_usage_and_writes_the_read_columns(self):
        """The actual is the sum of what the providers said they charged for, priced at
        the same catalog row the estimate used -- the whole reason the estimate-vs-actual
        gap means anything. It lands on the stage's own column and is returned, so a
        caller can answer without a second read. No warning fires when every result
        reported its usage: the alarm below has to stay worth reading."""
        from project.app.services.compose import estimate

        run = _make_run()
        results = [
            _result(input_tokens=20_000, output_tokens=4_000),
            _result(input_tokens=10_000, output_tokens=6_000),
        ]

        with self.assertNoLogs(ESTIMATE_LOGGER, level="WARNING"):
            actual = estimate.record_actuals(
                run, "read", results=results, provider=PROVIDER_KEY, model=PRIMARY_MODEL
            )

        expected = _price(30_000, 10_000, PRIMARY_IN, PRIMARY_OUT)
        self.assertEqual(actual, expected)
        self.assertIsInstance(actual, Decimal)
        self.assertEqual(actual.as_tuple().exponent, -4)

        run.refresh_from_db()
        self.assertEqual(run.read_cost_actual_usd, expected)
        self.assertEqual(run.read_provider, PROVIDER_KEY)
        self.assertEqual(run.read_model, PRIMARY_MODEL)
        self.assertIsNone(run.generate_cost_actual_usd)  # one stage, one column

    @unittest.expectedFailure
    def test_generate_actuals_are_priced_at_the_pair_that_actually_ran(self):
        """A stage can finish on a different model than the one quoted -- a provider
        substituting under load, or a reviewer who changed models after the estimate.
        The actual is priced at the row for the pair that ran and recorded beside it, or
        the cost history blames the wrong model for the bill."""
        from project.app.services.compose import estimate

        run = _make_run()
        results = [_result(input_tokens=5_000, output_tokens=2_500, model=OTHER_MODEL)]

        actual = estimate.record_actuals(
            run, "generate", results=results, provider=PROVIDER_KEY, model=OTHER_MODEL
        )

        self.assertEqual(actual, _price(5_000, 2_500, OTHER_IN, OTHER_OUT))
        # ...and specifically not the primary row's price, which is what a lookup
        # keyed on anything other than the pair passed in would produce.
        self.assertNotEqual(actual, _price(5_000, 2_500, PRIMARY_IN, PRIMARY_OUT))

        run.refresh_from_db()
        self.assertEqual(run.generate_cost_actual_usd, actual)
        self.assertEqual(run.generate_provider, PROVIDER_KEY)
        self.assertEqual(run.generate_model, OTHER_MODEL)
        self.assertIsNone(run.read_cost_actual_usd)  # the read stage never ran
        self.assertEqual(run.read_model, "")

    @unittest.expectedFailure
    def test_unobserved_token_counts_are_not_billed_and_are_not_silent(self):
        """``LLMResult.input_tokens`` defaults to ``None`` precisely so a result cannot
        claim a real observation of zero. The actuals inherit that stance: a missing
        count adds nothing to the sum, because there is no usage report to price, but
        the log says the total is short. An actual quietly missing a provider's usage is
        indistinguishable from a stage that ran cheap, and that is the number an
        operator would go on to trust."""
        from project.app.services.compose import estimate

        # An explicit id keeps the run's own number out of the digits matched below.
        run = _make_run(pk=501)
        priced = [_result(input_tokens=21_345, output_tokens=4_321) for _ in range(3)]
        half = _result(input_tokens=10_000, output_tokens=None)  # provider reported half
        blind = [_result() for _ in range(11)]  # caller-built; never observed usage at all

        with self.assertLogs(ESTIMATE_LOGGER, level="WARNING") as logs:
            actual = estimate.record_actuals(
                run,
                "read",
                results=[*priced, half, *blind],
                provider=PROVIDER_KEY,
                model=PRIMARY_MODEL,
            )

        # Only the counts that exist are priced -- three whole results plus one
        # half-reported input. Nothing is extrapolated over the gap, nothing crashes.
        self.assertEqual(actual, _price(3 * 21_345 + 10_000, 3 * 4_321, PRIMARY_IN, PRIMARY_OUT))
        run.refresh_from_db()
        self.assertEqual(run.read_cost_actual_usd, actual)

        first = "\n".join(logs.output)
        self.assertIn("read", first)  # which stage is under-reported
        # 12 of the 15 results carried no complete usage report, and nothing else on the
        # line can be a bare 12: the run is 501, the total 15, three results priced, the
        # money 11.2924. Matching a lone digit would have accepted any of those.
        self.assertRegex(first, r"\b12\b")

        # And the 12 tracks the count rather than just sitting there. Same run, same
        # stage, same priced results and so the same money -- only the unpriced count
        # moves, so a stray 12 from anywhere else would survive into this message too.
        with self.assertLogs(ESTIMATE_LOGGER, level="WARNING") as logs:
            estimate.record_actuals(
                run,
                "read",
                results=[*priced, half, *blind[:6]],
                provider=PROVIDER_KEY,
                model=PRIMARY_MODEL,
            )

        second = "\n".join(logs.output)
        self.assertRegex(second, r"\b7\b")
        self.assertNotRegex(second, r"\b12\b")


class EstimateEndpointTests(AuthenticatedAPITestCase):
    """``GET /api/runs/{id}/estimate/`` -- the price a reviewer sees before the
    button that spends."""

    @classmethod
    def setUpTestData(cls):
        _seed_catalog()
        cls.leads = [_make_lead(n) for n in (1, 2, 3)]

    def _get(self, run, **params):
        return self.client.get(f"/api/runs/{run.pk}/estimate/?{urlencode(params)}")

    @unittest.expectedFailure
    def test_the_endpoint_returns_the_estimate_with_money_as_a_decimal_string(self):
        """The wire carries as much of the promise as the type does: JSON has one number
        type and it is a float, so serializing ``usd_est`` as a number hands the browser
        a rounding error to render. ``REST_FRAMEWORK`` never sets
        ``COERCE_DECIMAL_TO_STRING``, so DRF's default string is what ships."""
        from project.app.services.compose import estimate

        run = _make_run()
        _add_run_lead(run, self.leads[0])

        resp = self._get(run, stage="read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["stage"], "read")
        self.assertEqual(body["provider"], PROVIDER_KEY)
        self.assertEqual(body["model"], PRIMARY_MODEL)
        self.assertEqual(body["lead_count"], 1)
        self.assertEqual(body["tokens_out_est"], estimate.READ_OUTPUT_TOKENS)
        self.assertIs(body["is_estimate"], True)
        self.assertIsInstance(body["usd_est"], str)  # never a JSON float
        self.assertEqual(
            Decimal(body["usd_est"]),
            _price(body["tokens_in_est"], body["tokens_out_est"], PRIMARY_IN, PRIMARY_OUT),
        )

    @unittest.expectedFailure
    def test_the_runs_cost_columns_cross_the_wire_as_strings_too(self):
        """The same settlement, one field on: the four ``*_cost_*_usd`` columns this
        module writes are the run summary's estimate-vs-actual line, and
        ``GenerateResult.actual_usd`` is the identical rule at the generate endpoint --
        all of them JSON strings the FE parses with ``Number()``. A float on any of them
        rounds the very number the comparison is made of. Unset stays ``null`` rather
        than ``"0.0000"``: "not yet quoted" is not "free"."""
        from project.app.services.compose import estimate

        run = _make_run()
        _add_run_lead(run, self.leads[0])
        est = estimate.estimate_stage(run, "read", provider=PROVIDER_KEY, model=PRIMARY_MODEL)
        actual = estimate.record_actuals(
            run,
            "read",
            results=[_result(input_tokens=21_345, output_tokens=4_321)],
            provider=PROVIDER_KEY,
            model=PRIMARY_MODEL,
        )

        body = self.client.get(f"/api/runs/{run.pk}/").json()

        self.assertIsInstance(body["read_cost_estimate_usd"], str)
        self.assertIsInstance(body["read_cost_actual_usd"], str)
        self.assertEqual(Decimal(body["read_cost_estimate_usd"]), est.usd_est)
        self.assertEqual(Decimal(body["read_cost_actual_usd"]), actual)
        self.assertIsNone(body["generate_cost_estimate_usd"])
        self.assertIsNone(body["generate_cost_actual_usd"])

    @unittest.expectedFailure
    def test_a_pair_that_is_not_in_the_catalog_is_a_400(self):
        """``LLMModel.DoesNotExist`` here is a client mistake -- an unknown model in the
        query string -- so it belongs on the 400 side of the boundary, not as a 500 in
        the logs. The envelope is ``views_queue.error()``'s: ``code`` is the slug
        ``client.ts`` branches on to say "unknown model" rather than "something went
        wrong", and an envelope keyed ``error`` is one the FE cannot branch on at all."""
        run = _make_run()
        _add_run_lead(run, self.leads[0])

        resp = self._get(run, stage="read", provider=PROVIDER_KEY, model="not-in-catalog")

        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["code"], "unknown_model")
        self.assertTrue(body["detail"])  # a sentence, not an empty placeholder

    @unittest.expectedFailure
    def test_a_stage_the_estimator_does_not_price_is_a_400(self):
        """An absent stage and an unrecognized stage are the same client error and get
        the same slug, so the FE has one branch to write rather than two. Neither may
        fall through to a default: quoting the read price on a generate button is worse
        than refusing."""
        run = _make_run()
        _add_run_lead(run, self.leads[0])

        for params in (
            {"stage": "classify", "provider": PROVIDER_KEY, "model": PRIMARY_MODEL},
            {"provider": PROVIDER_KEY, "model": PRIMARY_MODEL},
        ):
            with self.subTest(params=params):
                resp = self._get(run, **params)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()["code"], "invalid_stage")
                self.assertTrue(resp.json()["detail"])


class EstimateEndpointAuthTests(AuthenticatedAPITestCase):
    """The estimate endpoint reads the catalog and the run's lead set, both of which sit
    behind the global ``IsAuthenticated`` (MUS-37). A brand-new URL is exactly where that
    gets forgotten, and 401-not-403 is the pinned repo-wide shape --
    ``SessionAuthenticationWith401`` exists so the FE's route guard actually fires."""

    # Not @expectedFailure, and deliberately so: this one already passes at skeleton.
    # Authentication is framework-level, not component-level -- DRF answers the
    # anonymous request in initial(), before the stub view is entered -- so the
    # behaviour is real now and stays true when the estimate PR fills the view in.
    def test_the_estimate_endpoint_is_401_when_anonymous(self):
        # No run needs to exist: permissions run before the view looks the id up. A 404
        # here would mean the check sits downstream of the lookup and an anonymous
        # caller can probe run ids.
        self.client.logout()

        resp = self.client.get(
            f"/api/runs/1/estimate/?stage=read&provider={PROVIDER_KEY}&model={PRIMARY_MODEL}"
        )

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["code"], "not_authenticated")
