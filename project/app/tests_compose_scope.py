"""Component artifact: scope (MUS-47 component 2).

``services/compose/scope.py`` is the composer's only path from a JSON request body to a
queryset, so "never ``**kwargs`` into ``.filter()``" is the sentence this file turns into a
fact: an unknown key is refused *by name*, a structured value cannot smuggle a lookup, and a
value that will not coerce is an error rather than a guess.

The other half is arithmetic. Three of the four computed filters are about *absence* --
never contacted, never logged in, never signed up -- and two of those mean "maximally
overdue" while the third means "not in the window". An annotation that quietly drops NULL
rows gets all three wrong in the same silent direction: the run just comes out smaller and
nobody can tell which leads went missing. The component also owns
``POST /api/runs/preview-count/`` and the ``/api/scopes/`` family: the validator on the wire.

Planted red by the skeleton and green as of the scope component: the
``@unittest.expectedFailure`` markers are gone and nothing else in this file moved with
them. The in-body ``scope`` imports stay as written -- they are why the artifact still
collected while the component was a stub, and they cost nothing now that it is not.
:class:`UrlOrderingTests` never carried a marker at all: it pins ``urls.py``, which the
skeleton already shipped, so its job was always to stay green rather than to go green.
"""

import datetime

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse

from project.app.models import Event, Lead
from project.app.tests_auth_utils import AuthenticatedAPITestCase

# Frozen "today", so every day-count assertion is arithmetic rather than a race with the
# wall clock.
TODAY = datetime.date(2026, 6, 15)

# Spelled out rather than derived from FILTERABLE: the catalog test exists to catch
# FILTERABLE and docs/contracts/run-composer.md drifting apart, which a self-referential
# list could never do.
CONTRACT_KEYS = frozenset(
    "stage state book_min book_max producers_min producers_max years_min quotes_created_min "
    "quotes_created_max quotes_submitted_min quotes_submitted_max deals_min deals_max "
    "last_contacted_gt_days signed_up_within_days dormant_days has_notes".split()
)

KINDS = frozenset({"select", "int", "days", "bool"})
BOUNDS = frozenset({"gte", "lte", "exact", "days", "bool"})

# `<stem>_min` and `<stem>_max` deliberately share one label. The chip renderer composes
# label + bound into "book >= $50,000", so the label is a noun and the bound is direction.
TWIN_STEMS = ("book", "producers", "quotes_created", "quotes_submitted", "deals")

BIG, MID, SMALL = "sc_big", "sc_mid", "sc_small"
NEVER, GHOST, LURKER = "sc_never", "sc_ghost", "sc_lurker"
ALL_LEADS = frozenset({BIG, MID, SMALL, NEVER, GHOST, LURKER})


def _ago(days):
    return TODAY - datetime.timedelta(days=days)


def _noon(days_ago):
    """Event timestamps land at noon UTC.

    ``dormant_days`` compares a ``DateTimeField`` against a cutoff derived from a ``date``,
    and the two obvious spellings -- ``__date__lt``, or ``__lt`` a midnight datetime --
    disagree for events in the first or last hours of a day. At noon they agree.
    """
    return datetime.datetime.combine(
        _ago(days_ago), datetime.time(12, 0), tzinfo=datetime.timezone.utc
    )


def _lead(lead_id, **overrides):
    fields = dict(
        agency_name=f"Agency {lead_id}",
        contact_name=f"Contact {lead_id}",
        contact_email=f"{lead_id}@example.com",
        contact_phone="555-0100",
        state="CA",
        num_producers=3,
        years_in_business=5,
        estimated_book_size_usd=1_000_000,
        stage="active_trial",
        signed_up_date=None,
        last_login_date=None,
        quotes_created=0,
        quotes_submitted=0,
        deals_closed=0,
        last_contacted_date=None,
        hubspot_notes="",
    )
    fields.update(overrides)
    return Lead.objects.create(id=lead_id, **fields)


def _event(lead, type_, days_ago):
    return Event.objects.create(lead=lead, type=type_, timestamp=_noon(days_ago), meta={})


def _seed_fixture():
    """The six-lead table, as of ``TODAY`` = 2026-06-15.

    ==========  ==============  =====  ==========  =========  =====  ==========  ==========
    id          stage           state  book        contacted  up     notes       login events
    ==========  ==============  =====  ==========  =========  =====  ==========  ==========
    sc_big      active_trial    CA     25,000,000  -3d        -10d   yes         -1d
    sc_mid      active_trial    TX      5,000,000  -30d       -120d  ""          -30d
    sc_small    demo_completed  TX        250,000  -31d       -200d  whitespace  -45d
    sc_never    demo_completed  NY      1,000,000  NULL       NULL   yes         none at all
    sc_ghost    active_trial    NY      2,000,000  -29d       -60d   yes         -90d
    sc_lurker   active_trial    CA      3,000,000  -100d      -40d   yes         -95d, -2d
    ==========  ==============  =====  ==========  =========  =====  ==========  ==========

    ``sc_ghost`` and ``sc_lurker`` separate ``Lead.last_login_date`` from the login events,
    in opposite directions, and ghost carries a recent *non-login* event so a ``Max`` that
    forgets ``filter=Q(events__type="login")`` gets it wrong too. Every ``*_min``/``*_max``
    case below puts one lead exactly on the boundary, so inclusivity is pinned.
    """
    big = _lead(
        BIG,
        state="CA",
        stage="active_trial",
        estimated_book_size_usd=25_000_000,
        num_producers=12,
        years_in_business=20,
        quotes_created=40,
        quotes_submitted=30,
        deals_closed=9,
        last_contacted_date=_ago(3),
        signed_up_date=_ago(10),
        last_login_date=_ago(1),
        hubspot_notes="Renewal conversation booked for Q3.",
    )
    _event(big, "login", 1)

    mid = _lead(
        MID,
        state="TX",
        stage="active_trial",
        estimated_book_size_usd=5_000_000,
        num_producers=5,
        years_in_business=8,
        quotes_created=12,
        quotes_submitted=6,
        deals_closed=2,
        last_contacted_date=_ago(30),
        signed_up_date=_ago(120),
        last_login_date=_ago(14),
        hubspot_notes="",
    )
    _event(mid, "login", 30)  # the dormancy boundary lead, at exactly 30 days

    small = _lead(
        SMALL,
        state="TX",
        stage="demo_completed",
        estimated_book_size_usd=250_000,
        num_producers=1,
        years_in_business=2,
        last_contacted_date=_ago(31),
        signed_up_date=_ago(200),
        last_login_date=_ago(45),
        hubspot_notes="   \n  ",  # whitespace is not a note
    )
    _event(small, "login", 45)

    # Every absence at once, and no events at all.
    _lead(
        NEVER,
        state="NY",
        stage="demo_completed",
        estimated_book_size_usd=1_000_000,
        num_producers=3,
        years_in_business=5,
        quotes_created=2,
        hubspot_notes="Left a voicemail with the front desk.",
    )

    ghost = _lead(
        GHOST,
        state="NY",
        stage="active_trial",
        estimated_book_size_usd=2_000_000,
        num_producers=4,
        years_in_business=6,
        quotes_created=5,
        quotes_submitted=3,
        deals_closed=1,
        last_contacted_date=_ago(29),
        signed_up_date=_ago(60),
        last_login_date=_ago(2),  # the column lies
        hubspot_notes="Asked about pricing tiers.",
    )
    _event(ghost, "login", 90)
    _event(ghost, "email_sent", 1)  # recent, but not a login

    lurker = _lead(
        LURKER,
        state="CA",
        stage="active_trial",
        estimated_book_size_usd=3_000_000,
        num_producers=6,
        years_in_business=11,
        quotes_created=8,
        quotes_submitted=4,
        deals_closed=1,
        last_contacted_date=_ago(100),
        signed_up_date=_ago(40),
        last_login_date=_ago(95),  # the column lies the other way
        hubspot_notes="Quiet since the pilot.",
    )
    _event(lurker, "login", 95)
    _event(lurker, "login", 2)  # Max(), not Min()


class ScopeValidationTests(SimpleTestCase):
    """``validate_scope`` -- the whitelist and the coercion table.

    ``SimpleTestCase`` because it actually blocks queries, which is half the claim. Every
    rejection checks ``ScopeError.key``: the 400 is ``{"code": ..., "detail": ..., "key":
    ...}``, and an exception that cannot name the filter turns a fixable error into a shrug.
    """

    def test_an_unknown_key_is_rejected_and_named(self):
        from project.app.services.compose import scope

        unknown = (
            "stages",  # the near-miss an operator or an FE typo produces
            "hubspot_notes__icontains",  # a real column wearing a real lookup
            "id__in",
            "events__lead__id",  # a relation walk out of the Lead table
            "stage__in",  # a *known* key wearing a lookup suffix is still not that key
            "__class__",
        )
        for key in unknown:
            with self.subTest(key=key):
                with self.assertRaises(scope.ScopeError) as caught:
                    scope.validate_scope({key: 1})
                self.assertEqual(caught.exception.key, key)
                # ScopeError(ValueError), so a caller that only knows about ValueError
                # still fails closed rather than passing it on.
                self.assertIsInstance(caught.exception, ValueError)

    def test_one_unknown_key_poisons_the_whole_scope(self):
        """Dropping the one extra key runs against a wider lead set than the operator saw."""
        from project.app.services.compose import scope

        with self.assertRaises(scope.ScopeError) as caught:
            scope.validate_scope({"stage": "active_trial", "id__in": [BIG]})
        self.assertEqual(caught.exception.key, "id__in")

    def test_numeric_values_coerce_from_digit_strings_and_reject_everything_else(self):
        from project.app.services.compose import scope

        self.assertEqual(scope.validate_scope({"book_min": 50000}), {"book_min": 50000})
        # Query strings and HTML number inputs both arrive as text, so the digit string is
        # a first-class input, not a fallback.
        coerced = scope.validate_scope({"book_min": "50000"})
        self.assertEqual(coerced, {"book_min": 50000})
        self.assertIsInstance(coerced["book_min"], int)
        # bool is an int subclass: a coercer built on int() takes `true` for 1 and turns
        # "book over a million" into "book over a dollar".
        self.assertNotIsInstance(coerced["book_min"], bool)
        # Zero is a legal bound, not "unset" -- a truthiness check silently drops
        # `deals_max: 0`, the filter for "has closed nothing".
        self.assertEqual(scope.validate_scope({"deals_max": 0}), {"deals_max": 0})

        rejected = (
            "abc",
            "",
            "   ",
            None,
            True,  # see above
            False,
            1.5,  # int(1.5) truncates: answers a question nobody asked
            "50_000",  # CPython's int() accepts it; no JSON client emits it
            "1e5",
            -1,  # every numeric field in this catalog counts something
            "-1",
            [50000],
            {"value": 50000},
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(scope.ScopeError) as caught:
                    scope.validate_scope({"book_min": value})
                self.assertEqual(caught.exception.key, "book_min")

    def test_a_structured_value_cannot_smuggle_a_lookup(self):
        """The shape that beats a validator checking keys and handing the value to the ORM."""
        from project.app.services.compose import scope

        for value in (
            {"__gt": 1},
            {"stage__in": ["active_trial"]},
            ["active_trial", "demo_completed"],
            ("active_trial",),
        ):
            with self.subTest(value=value):
                with self.assertRaises(scope.ScopeError) as caught:
                    scope.validate_scope({"stage": value})
                self.assertEqual(caught.exception.key, "stage")

        with self.assertRaises(scope.ScopeError) as caught:
            scope.validate_scope({"book_min": {"__gt": 1}})
        self.assertEqual(caught.exception.key, "book_min")

    def test_has_notes_takes_json_booleans_and_refuses_to_guess_at_anything_else(self):
        """ "yes" read as falsey selects the exact complement, invisibly: different leads."""
        from project.app.services.compose import scope

        self.assertIs(scope.validate_scope({"has_notes": True})["has_notes"], True)
        self.assertIs(scope.validate_scope({"has_notes": False})["has_notes"], False)
        for text, expected in (
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("false", False),
            ("False", False),
        ):
            with self.subTest(text=text):
                self.assertIs(scope.validate_scope({"has_notes": text})["has_notes"], expected)

        for ambiguous in ("yes", "no", "y", "n", "on", "off", "1", "0", 1, 0, "", None, []):
            with self.subTest(value=ambiguous):
                with self.assertRaises(scope.ScopeError) as caught:
                    scope.validate_scope({"has_notes": ambiguous})
                self.assertEqual(caught.exception.key, "has_notes")

    def test_day_windows_are_non_negative_whole_numbers(self):
        from project.app.services.compose import scope

        for key in ("last_contacted_gt_days", "signed_up_within_days", "dormant_days"):
            with self.subTest(key=key):
                self.assertEqual(scope.validate_scope({key: 30})[key], 30)
                self.assertEqual(scope.validate_scope({key: "30"})[key], 30)
                # Zero is a window ("more than 0 days ago"), not a missing filter.
                self.assertEqual(scope.validate_scope({key: 0})[key], 0)
                for bad in (-1, "-1", 0.5, "abc", True, None):
                    with self.subTest(value=bad):
                        with self.assertRaises(scope.ScopeError) as caught:
                            scope.validate_scope({key: bad})
                        self.assertEqual(caught.exception.key, key)

    def test_select_values_are_checked_against_the_column_they_filter(self):
        """An empty run for a typo'd stage is indistinguishable from "no leads match"."""
        from project.app.services.compose import scope

        self.assertEqual(scope.validate_scope({"stage": "active_trial"}), {"stage": "active_trial"})
        for bad in ("not_a_stage", "active", "", "ACTIVE_TRIAL"):
            with self.subTest(stage=bad):
                with self.assertRaises(scope.ScopeError) as caught:
                    scope.validate_scope({"stage": bad})
                self.assertEqual(caught.exception.key, "stage")

        # `Lead.state` stores "CA" and someone will type "ca".
        self.assertEqual(scope.validate_scope({"state": "ca"}), {"state": "CA"})
        self.assertEqual(scope.validate_scope({"state": "TX"}), {"state": "TX"})
        for bad in ("California", "C", "", "C A", 5, None):
            with self.subTest(state=bad):
                with self.assertRaises(scope.ScopeError) as caught:
                    scope.validate_scope({"state": bad})
                self.assertEqual(caught.exception.key, "state")

    def test_validation_returns_a_fresh_dict_and_leaves_the_request_body_alone(self):
        """``SavedScope.filters`` stores the return value; the view still owns the raw body."""
        from project.app.services.compose import scope

        self.assertEqual(scope.validate_scope({}), {})  # scoping is an offer

        raw = {"stage": "active_trial", "book_min": "50000", "state": "tx"}
        out = scope.validate_scope(raw)
        self.assertIsNot(out, raw)
        self.assertEqual(raw, {"stage": "active_trial", "book_min": "50000", "state": "tx"})
        self.assertEqual(out, {"stage": "active_trial", "book_min": 50000, "state": "TX"})


class ScopeCatalogTests(SimpleTestCase):
    """``scope_field_catalog()`` -- the field list ``GET /api/scopes/fields/`` serves.

    The catalog and the validator are one whitelist seen from two directions. Drift means
    the UI offers a filter the API refuses, or hides one it accepts.
    """

    def test_the_catalog_the_validator_and_the_contract_all_carry_the_same_keys(self):
        """Uniqueness is asserted on ``key`` and nowhere else -- see the twins test below."""
        from project.app.services.compose import scope

        keys = [entry["key"] for entry in scope.scope_field_catalog()]
        self.assertEqual(len(keys), len(set(keys)))  # one entry per key, never two
        self.assertEqual(set(keys), set(scope.FILTERABLE))
        self.assertEqual(set(keys), set(CONTRACT_KEYS))

    def test_every_entry_is_a_projection_of_its_filter_spec(self):
        """A catalog maintained beside ``FILTERABLE`` starts lying the first time one moves."""
        from project.app.services.compose import scope

        catalog = scope.scope_field_catalog()
        for entry in catalog:
            with self.subTest(key=entry["key"]):
                # Exactly the FE `ScopeField` fields -- `coerce` is a callable and never
                # crosses the wire.
                self.assertEqual(set(entry), {"key", "label", "bound", "kind", "choices"})
                spec = scope.FILTERABLE[entry["key"]]
                self.assertIsInstance(spec, scope.FilterSpec)
                self.assertEqual(entry["label"], spec.label)
                self.assertEqual(entry["bound"], spec.bound)
                self.assertEqual(entry["kind"], spec.kind)
                self.assertEqual(tuple(entry["choices"]), tuple(spec.choices))
                self.assertIn(entry["kind"], KINDS)
                self.assertIn(entry["bound"], BOUNDS)
                self.assertTrue(entry["label"].strip())
                if entry["kind"] != "select":
                    # A day count with a dropdown is a rendering bug waiting to ship.
                    self.assertEqual(tuple(entry["choices"]), ())

        kinds = {entry["key"]: entry["kind"] for entry in catalog}
        bounds = {entry["key"]: entry["bound"] for entry in catalog}
        for key, kind, bound in (
            ("stage", "select", "exact"),
            ("state", "select", "exact"),
            ("book_min", "int", "gte"),
            ("book_max", "int", "lte"),
            ("years_min", "int", "gte"),
            ("deals_max", "int", "lte"),
            ("last_contacted_gt_days", "days", "days"),
            ("signed_up_within_days", "days", "days"),
            ("dormant_days", "days", "days"),
            ("has_notes", "bool", "bool"),
        ):
            with self.subTest(key=key):
                self.assertEqual(kinds[key], kind)
                self.assertEqual(bounds[key], bound)

        stage = next(entry for entry in catalog if entry["key"] == "stage")
        self.assertEqual(tuple(stage["choices"]), ("active_trial", "demo_completed"))

    def test_min_max_twins_share_a_label_and_are_told_apart_by_bound(self):
        """What the chip renderer depends on, and why uniqueness is pinned on ``key``."""
        from project.app.services.compose import scope

        catalog = scope.scope_field_catalog()
        labels = {entry["key"]: entry["label"] for entry in catalog}
        bounds = {entry["key"]: entry["bound"] for entry in catalog}

        self.assertEqual(labels["book_min"], "book")  # the contract's worked example
        self.assertEqual(labels["book_max"], "book")

        for stem in TWIN_STEMS:
            low, high = f"{stem}_min", f"{stem}_max"
            with self.subTest(stem=stem):
                self.assertEqual(labels[low], labels[high])
                self.assertEqual(bounds[low], "gte")
                self.assertEqual(bounds[high], "lte")
                # The label carries no direction of its own: a label of "book min" would
                # render "book min >= $50,000", saying the bound twice and once badly.
                for word in ("min", "max", ">", "<"):
                    self.assertNotIn(word, labels[low].lower())


class ScopeQueryTests(TestCase):
    """``apply_scope`` over :func:`_seed_fixture`, asserted as exact id sets.

    Counts alone would pass for a filter off by one lead in each direction, which is exactly
    what a wrong NULL rule looks like.
    """

    @classmethod
    def setUpTestData(cls):
        _seed_fixture()

    @staticmethod
    def _scoped(raw):
        from project.app.services.compose import scope

        queryset = scope.apply_scope(Lead.objects.all(), scope.validate_scope(raw), today=TODAY)
        return set(queryset.values_list("id", flat=True))

    def test_an_empty_scope_returns_every_lead(self):
        """Scoping is an offer, not a requirement: "everyone" is the default run."""
        self.assertEqual(self._scoped({}), set(ALL_LEADS))

    def test_each_column_filter_narrows_to_exactly_its_leads(self):
        cases = (
            # An exact match, not a contains/startswith: "active" must find nothing.
            ({"stage": "active_trial"}, {BIG, MID, GHOST, LURKER}),
            ({"stage": "demo_completed"}, {SMALL, NEVER}),
            ({"state": "CA"}, {BIG, LURKER}),
            ({"state": "ca"}, {BIG, LURKER}),  # normalized on the way in
            ({"state": "NY"}, {NEVER, GHOST}),
            ({"book_min": 3_000_000}, {BIG, MID, LURKER}),  # lurker is exactly 3M
            ({"book_max": 1_000_000}, {SMALL, NEVER}),  # never is exactly 1M
            ({"producers_min": 6}, {BIG, LURKER}),  # lurker is exactly 6
            ({"producers_max": 3}, {SMALL, NEVER}),  # never is exactly 3
            ({"years_min": 11}, {BIG, LURKER}),  # lurker is exactly 11
            ({"quotes_created_min": 8}, {BIG, MID, LURKER}),
            ({"quotes_created_max": 2}, {SMALL, NEVER}),
            ({"quotes_submitted_min": 4}, {BIG, MID, LURKER}),
            ({"quotes_submitted_max": 3}, {SMALL, NEVER, GHOST}),
            ({"deals_min": 1}, {BIG, MID, GHOST, LURKER}),
            # Zero as a bound, not as "no filter": the leads that closed nothing.
            ({"deals_max": 0}, {SMALL, NEVER}),
        )
        for raw, expected in cases:
            with self.subTest(scope=raw):
                self.assertEqual(self._scoped(raw), expected)

    def test_last_contacted_gt_days_counts_never_contacted_as_the_most_overdue(self):
        """``last_contacted_date__lt=cutoff`` drops the NULL -- the one lead most wanted."""

        def scoped(days):
            return self._scoped({"last_contacted_gt_days": days})

        # sc_small at 31d and sc_lurker at 100d are over; sc_never has never been contacted
        # at all; sc_mid sits exactly on 30 and "gt" excludes it.
        self.assertEqual(scoped(30), {SMALL, NEVER, LURKER})
        self.assertIn(NEVER, scoped(30))  # the NULL, named explicitly
        self.assertNotIn(MID, scoped(30))  # strictly greater than, at the boundary
        self.assertEqual(scoped(29), {MID, SMALL, NEVER, LURKER})  # one day later, mid joins
        # The NULL is unconditional: overdue at every window, including one no dated lead
        # can satisfy.
        self.assertEqual(scoped(3650), {NEVER})

    def test_dormant_days_follows_the_login_events_not_the_lead_column(self):
        """``Lead.last_login_date`` is a denormalized CRM column; the events are the record."""

        def scoped(days):
            return self._scoped({"dormant_days": days})

        dormant = scoped(30)
        self.assertIn(GHOST, dormant)  # column says 2 days; last login event was 90
        self.assertNotIn(LURKER, dormant)  # column says 95 days; logged in 2 days ago
        # No login events at all is maximally dormant, for the same reason a NULL
        # last_contacted_date is maximally overdue: the annotation is NULL, the row lives.
        self.assertIn(NEVER, dormant)
        self.assertEqual(dormant, {SMALL, NEVER, GHOST})
        self.assertNotIn(MID, dormant)  # login exactly 30 days ago: strictly greater
        self.assertEqual(scoped(29), {MID, SMALL, NEVER, GHOST})

    def test_signed_up_within_days_is_a_closed_window_over_a_real_signup(self):
        """The opposite NULL rule: no ``signed_up_date`` did not sign up inside the window."""

        def scoped(days):
            return self._scoped({"signed_up_within_days": days})

        self.assertEqual(scoped(60), {BIG, GHOST, LURKER})
        self.assertNotIn(NEVER, scoped(60))
        self.assertIn(GHOST, scoped(60))  # exactly 60 days: the window is closed
        self.assertEqual(scoped(59), {BIG, LURKER})
        self.assertEqual(scoped(3650), {BIG, MID, SMALL, GHOST, LURKER})  # still never NEVER

    def test_has_notes_partitions_the_table_and_whitespace_is_not_a_note(self):
        """Three spaces give the model nothing to read, and the two sides must cover the
        table exactly or "has notes" plus "has none" adds up to fewer leads than exist."""
        with_notes = self._scoped({"has_notes": True})
        without_notes = self._scoped({"has_notes": False})

        self.assertEqual(with_notes, {BIG, NEVER, GHOST, LURKER})
        self.assertEqual(without_notes, {MID, SMALL})  # "" and "   \n  "
        self.assertEqual(with_notes | without_notes, set(ALL_LEADS))
        self.assertEqual(with_notes & without_notes, set())

    def test_filters_intersect_rather_than_accumulate(self):
        """Chips read as "and", including across the annotations most likely to be applied
        to a fresh queryset instead of the narrowed one."""
        self.assertEqual(self._scoped({"stage": "active_trial", "state": "CA"}), {BIG, LURKER})
        self.assertEqual(
            self._scoped({"stage": "active_trial", "state": "CA", "deals_min": 5}), {BIG}
        )
        # Plain column AND computed: notes to read, and overdue for contact.
        self.assertEqual(
            self._scoped({"has_notes": True, "last_contacted_gt_days": 30}), {NEVER, LURKER}
        )
        # A combination nothing satisfies is empty, not everything.
        self.assertEqual(self._scoped({"state": "NY", "book_min": 10_000_000}), set())

    def test_apply_scope_refuses_a_key_that_never_went_through_the_validator(self):
        """ "Already validated" is a comment; the refusal happens at the call, not at
        queryset evaluation, so a bad scope never becomes a lazy queryset someone passes on."""
        from project.app.services.compose import scope

        with self.assertRaises(scope.ScopeError) as caught:
            scope.apply_scope(
                Lead.objects.all(), {"hubspot_notes__icontains": "pricing"}, today=TODAY
            )
        self.assertEqual(caught.exception.key, "hubspot_notes__icontains")


class ScopeQueryCountTests(TestCase):
    """ "Annotations, not Python loops" -- pinned, because it only ever fails silently.

    A comprehension walking ``lead.events.all()`` per lead gives identical results on a
    six-lead fixture and turns a 200-lead preview into 200 queries. The constant is 1,
    because evaluating one queryset is one SELECT.
    """

    # All four computed filters at once, plus two plain ones, so the annotation is combined
    # with ordinary predicates the way a real scope combines them.
    COMPUTED = {
        "last_contacted_gt_days": 7,
        "signed_up_within_days": 400,
        "dormant_days": 3,
        "has_notes": True,
        "stage": "active_trial",
        "book_min": 1,
    }

    @staticmethod
    def _uniform_leads(count, offset=0):
        """Leads that all pass ``COMPUTED``, so the result set scales with N."""
        for index in range(offset, offset + count):
            lead = _lead(
                f"sc_u{index:04d}",
                last_contacted_date=_ago(30),
                signed_up_date=_ago(120),
                last_login_date=_ago(1),  # deliberately fresh; the events disagree
                hubspot_notes="Follow up after the pilot review.",
            )
            _event(lead, "login", 40)
            _event(lead, "login", 35)
            _event(lead, "email_sent", 2)

    def test_the_computed_filters_cost_one_query_at_any_lead_count(self):
        from project.app.services.compose import scope

        validated = scope.validate_scope(dict(self.COMPUTED))

        self._uniform_leads(3)
        with CaptureQueriesContext(connection) as captured:
            rows = list(scope.apply_scope(Lead.objects.all(), validated, today=TODAY))
        self.assertEqual(len(rows), 3)
        cost = len(captured.captured_queries)
        self.assertEqual(cost, 1)  # one SELECT, annotations and all

        self._uniform_leads(27, offset=3)
        with self.assertNumQueries(cost):  # the same number, ten times the leads
            rows = list(scope.apply_scope(Lead.objects.all(), validated, today=TODAY))
        self.assertEqual(len(rows), 30)


class UrlOrderingTests(SimpleTestCase):
    """``/api/scopes/fields/`` must not be swallowed by ``/api/scopes/{id}/``.

    The one test here that is **not** ``expectedFailure``: ``urls.py`` ships with the
    skeleton, so this is green today and its job is to stay green. ``<int:pk>`` happens to
    make the ordering safe; the ordering is what the contract promises, and a later
    ``<str:pk>`` would turn that accident into a silent 404.
    """

    def test_scope_fields_resolves_to_its_own_view_and_precedes_the_detail_route(self):
        from project.app import urls as app_urls
        from project.app.views_compose import ScopeDetailView, ScopeFieldsView

        self.assertEqual(reverse("scope-fields"), "/api/scopes/fields/")
        self.assertIs(resolve("/api/scopes/fields/").func.cls, ScopeFieldsView)
        self.assertIs(resolve("/api/scopes/7/").func.cls, ScopeDetailView)

        names = [pattern.name for pattern in app_urls.urlpatterns]
        self.assertLess(names.index("scope-fields"), names.index("scope-detail"))


class ScopeFieldsEndpointTests(AuthenticatedAPITestCase):
    """``GET /api/scopes/fields/`` -- the catalog, on the wire.

    Asserted through ``response.json()`` rather than ``response.data``: the FE reads
    rendered JSON, and a tuple that never survives serialization is a bug the Python-side
    catalog test cannot see.
    """

    def test_the_catalog_is_served_under_a_fields_key(self):
        response = self.client.get("/api/scopes/fields/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        # An envelope, not a bare list: room to add a key without breaking the FE.
        self.assertEqual(set(body), {"fields"})
        self.assertIsInstance(body["fields"], list)
        self.assertEqual({entry["key"] for entry in body["fields"]}, set(CONTRACT_KEYS))

    def test_every_entry_matches_the_frontend_ScopeField_type(self):
        """A field ``types.ts`` reads and the serializer omits only shows up in the browser."""
        response = self.client.get("/api/scopes/fields/")

        self.assertEqual(response.status_code, 200)
        for entry in response.json()["fields"]:
            with self.subTest(key=entry["key"]):
                self.assertEqual(set(entry), {"key", "label", "bound", "kind", "choices"})
                self.assertIsInstance(entry["key"], str)
                self.assertIsInstance(entry["label"], str)
                self.assertIn(entry["bound"], BOUNDS)
                self.assertIn(entry["kind"], KINDS)
                self.assertIsInstance(entry["choices"], list)  # JSON array, never a tuple
                self.assertTrue(all(isinstance(choice, str) for choice in entry["choices"]))


class PreviewCountEndpointTests(AuthenticatedAPITestCase):
    """``POST /api/runs/preview-count/`` -- the number under the Create Run button.

    The validator seen from the wire, and the only compose endpoint that spends nothing,
    writes nothing and cares nothing about the lifecycle. Fixture: :func:`_seed_fixture`,
    six leads, four of them ``active_trial``.
    """

    @classmethod
    def setUpTestData(cls):
        _seed_fixture()

    def _preview(self, scope_body):
        return self.client.post(
            "/api/runs/preview-count/",
            data={"scope": scope_body},
            content_type="application/json",
        )

    def test_preview_reports_the_scoped_count_beside_the_table_total(self):
        """``total`` is what makes ``count`` legible: "4" alone does not say of how many."""
        response = self._preview({"stage": "active_trial"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"count": 4, "total": 6})

        empty = self._preview({})
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), {"count": 6, "total": 6})

        # A scope nothing matches is a legitimate zero, not a 404: it is the answer that
        # stops the operator creating an empty run.
        none = self._preview({"state": "NY", "book_min": 10_000_000})
        self.assertEqual(none.status_code, 200)
        self.assertEqual(none.json()["count"], 0)

    def test_previewing_creates_no_run(self):
        """A run created here would take the single active slot the operator was still
        deciding how to fill."""
        from project.app.models import PlannerRun, RunLead

        self.assertEqual(self._preview({"stage": "active_trial"}).status_code, 200)
        self.assertEqual(PlannerRun.objects.count(), 0)
        self.assertEqual(RunLead.objects.count(), 0)

    def test_preview_answers_the_same_whether_or_not_a_run_is_active(self):
        """``POST /api/runs/`` 409s while a run is active; preview must not, because
        re-scoping is exactly what an operator does *with* a run open."""
        from project.app.models import PlannerRun

        before = self._preview({"stage": "active_trial"}).json()

        run = PlannerRun.objects.create(scope={}, created_by=self.TEST_EMAIL)
        response = self._preview({"stage": "active_trial"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), before)
        run.refresh_from_db()
        self.assertEqual(run.status, PlannerRun.STATUS_DRAFT)  # untouched, not re-scoped
        self.assertEqual(PlannerRun.objects.count(), 1)

    def test_an_invalid_scope_is_a_400_naming_the_offending_filter(self):
        """``ScopeError.key`` reaches the wire, so the FE can point at the chip."""
        from project.app.models import PlannerRun

        unknown = self._preview({"stage": "active_trial", "id__in": [BIG]})
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["code"], "unknown_filter")
        self.assertEqual(unknown.json()["key"], "id__in")
        self.assertIn("detail", unknown.json())

        uncoercible = self._preview({"book_min": "a lot"})
        self.assertEqual(uncoercible.status_code, 400)
        self.assertEqual(uncoercible.json()["code"], "invalid_filter")
        self.assertEqual(uncoercible.json()["key"], "book_min")

        self.assertEqual(PlannerRun.objects.count(), 0)


class SavedScopeEndpointTests(AuthenticatedAPITestCase):
    """``/api/scopes/`` -- list, save, forget.

    ``SavedScope.filters`` goes through ``validate_scope`` on write "so a stored scope
    cannot smuggle a key past the validator later": the row is replayed into ``apply_scope``
    on some future run, and validating only then leaves it looking legitimate meanwhile.
    """

    def _post(self, name, filters):
        return self.client.post(
            "/api/scopes/",
            data={"name": name, "filters": filters},
            content_type="application/json",
        )

    def test_a_saved_scope_cannot_store_a_key_the_validator_would_reject(self):
        from project.app.models import SavedScope

        response = self._post("Texas books", {"state": "TX", "id__in": [BIG]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "unknown_filter")
        self.assertEqual(response.json()["key"], "id__in")  # named, so it is fixable
        self.assertEqual(SavedScope.objects.count(), 0)  # nothing partially stored

    def test_a_saved_scope_with_an_uncoercible_value_is_refused_too(self):
        """A known key with a value that will never coerce is as unusable stored as live."""
        from project.app.models import SavedScope

        response = self._post("Big books", {"book_min": "a lot"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_filter")
        self.assertEqual(response.json()["key"], "book_min")
        self.assertEqual(SavedScope.objects.count(), 0)

    def test_a_saved_scope_stores_the_coerced_filters_not_the_raw_body(self):
        """Storing the raw body pushes coercion onto every future reader."""
        from project.app.models import SavedScope

        response = self._post("Texas books", {"state": "tx", "book_min": "50000"})

        self.assertEqual(response.status_code, 201)
        saved = SavedScope.objects.get(name="Texas books")
        self.assertEqual(saved.filters, {"state": "TX", "book_min": 50000})
        # The 201 body goes straight into the FE's list, so it carries the same coerced
        # filters and the id the delete route needs.
        self.assertEqual(response.json()["id"], saved.pk)
        self.assertEqual(response.json()["filters"], {"state": "TX", "book_min": 50000})

    def test_the_list_serves_every_saved_scope_in_name_order(self):
        """``Meta.ordering`` is ``["name"]``; rows carry the FE ``SavedScope`` fields."""
        self.assertEqual(self._post("Texas books", {"state": "TX"}).status_code, 201)
        self.assertEqual(self._post("Any lead", {}).status_code, 201)

        response = self.client.get("/api/scopes/")

        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual([row["name"] for row in rows], ["Any lead", "Texas books"])
        self.assertEqual(set(rows[0]), {"id", "name", "filters", "created_at", "created_by"})
        self.assertEqual(rows[0]["filters"], {})  # an empty scope is a scope
        self.assertEqual(rows[1]["filters"], {"state": "TX"})

    def test_deleting_a_scope_forgets_that_one_and_leaves_the_rest(self):
        from project.app.models import SavedScope

        doomed = self._post("Texas books", {"state": "TX"}).json()["id"]
        self.assertEqual(self._post("Any lead", {}).status_code, 201)

        response = self.client.delete(f"/api/scopes/{doomed}/")

        self.assertEqual(response.status_code, 204)  # no body; the FE drops the row it sent
        self.assertFalse(SavedScope.objects.filter(pk=doomed).exists())
        self.assertEqual([s.name for s in SavedScope.objects.all()], ["Any lead"])

    def test_deleting_a_scope_that_is_not_there_is_a_404_with_a_slug(self):
        """Two tabs, one scope, one delete each: the second gets a branchable answer."""
        response = self.client.delete("/api/scopes/4242/")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")
        self.assertIn("detail", response.json())
