#!/usr/bin/env python3
"""Rules regression eval for the lead classifier (MUS-20).

`determine_priority` / `determine_action` in ``project.app.services.outreach``
are the actual product -- the LLM only writes copy. They are tuned by
hand-picked threshold constants (``DORMANT_DAYS``, ``POWER_USER_DEALS``, ...)
with no measurement of whether the *classification* is correct. This harness
scores the classifier against a hand-labeled golden dataset of ground-truth
answers, prints a per-action-type score table + confusion matrix, and fails
(exit 1) if any action type regresses below a committed baseline.

Pure Python: no Django, no database, no network. The classifier duck-types on
lead-like objects, so we feed it ``SimpleNamespace`` stubs built from
``evals/golden/leads.jsonl`` and the whole run finishes in milliseconds.

Usage::

    python evals/run_rules_eval.py                    # score + gate vs baseline
    python evals/run_rules_eval.py --update-baseline  # (re)write the baseline
    python evals/run_rules_eval.py --golden PATH       # score a different set
"""

import argparse
import datetime
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Make ``project.app.services`` importable no matter where this is run from.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from project.app.services import actions, outreach  # noqa: E402

EVALS_DIR = Path(__file__).resolve().parent
GOLDEN_PATH = EVALS_DIR / "golden" / "leads.jsonl"
BASELINE_PATH = EVALS_DIR / "baselines" / "rules.json"

# Frozen "today" so every date-based rule is deterministic. Integer date fields
# in the golden file are read as "this many days before TODAY" (see _coerce_date),
# which makes threshold-boundary cases self-documenting: ``"last_login_date": 21``
# means exactly DORMANT_DAYS days ago.
TODAY = datetime.date(2026, 6, 12)

ACTION_TYPES = list(actions.ACTION_TYPES)
VALID_PRIORITIES = (1, 2, 3)

# Metrics are stored/compared rounded to this many decimals; the gate tolerance
# only has to absorb floating-point noise because a genuine per-action change is
# at least 1/support (>= ~0.02 for this dataset), far above ROUND/EPSILON.
ROUND_NDIGITS = 4
EPSILON = 1e-9

# Short codes for the confusion-matrix header (six columns don't fit full names).
ACTION_CODE = {
    actions.POWER_USER_REWARD: "PUR",
    actions.FOLLOW_UP_AFTER_HOLD: "FUH",
    actions.REENGAGE_DORMANT: "RED",
    actions.NUDGE_USAGE: "NUD",
    actions.COMPLETE_ONBOARDING: "ONB",
    actions.UNKNOWN: "UNK",
}

# Lead field defaults mirror project/app/tests_logic.py::_lead so a golden record
# only needs to specify the fields that matter for its case; everything else
# behaves like the real stubs.
LEAD_DEFAULTS = dict(
    id="gold_x",
    agency_name="Agency",
    contact_name="Contact",
    contact_email="contact@example.com",
    contact_phone="555-0000",
    state="CO",
    num_producers=1,
    years_in_business=1,
    estimated_book_size_usd=0,
    stage="active_trial",
    signed_up_date=None,
    last_login_date=None,
    quotes_created=0,
    quotes_submitted=0,
    deals_closed=0,
    last_contacted_date=None,
    hubspot_notes="",
)
DATE_FIELDS = ("signed_up_date", "last_login_date", "last_contacted_date")
ALLOWED_LEAD_KEYS = set(LEAD_DEFAULTS) | {"events"}


# ---------------------------------------------------------------------------
# lead construction (duck-typed, mirrors project/app/tests_logic.py)
# ---------------------------------------------------------------------------

class _EventSet:
    """Duck-types a Django related manager (``lead.events.all()``)."""

    def __init__(self, events):
        self._events = list(events)

    def all(self):
        return list(self._events)


def _coerce_date(value, ctx):
    """ISO string -> date; int N -> N days before TODAY; None -> None."""
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise ValueError(f"{ctx}: date field must not be a bool ({value!r})")
    if isinstance(value, int):
        return TODAY - datetime.timedelta(days=value)
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{ctx}: bad ISO date {value!r}: {exc}") from exc
    raise ValueError(f"{ctx}: unsupported date value {value!r}")


def _build_event(raw, ctx):
    ts = raw.get("timestamp")
    if isinstance(ts, str):
        try:
            ts = datetime.datetime.fromisoformat(ts)
        except ValueError:
            ts = datetime.datetime.combine(_coerce_date(ts, ctx), datetime.time(9))
    elif ts is None:
        # Classifiers never read event timestamps (only the copy path does), but
        # keep the attribute present so any accidental access doesn't blow up.
        ts = datetime.datetime.combine(TODAY, datetime.time(9))
    return SimpleNamespace(type=raw.get("type", ""), timestamp=ts, meta=raw.get("meta") or {})


def build_lead(record):
    """Turn one golden record's ``lead`` block into a duck-typed lead object."""
    raw = dict(record.get("lead", {}))
    ctx = record.get("id", "<no-id>")

    unknown = set(raw) - ALLOWED_LEAD_KEYS
    if unknown:
        raise ValueError(f"{ctx}: unknown lead field(s): {', '.join(sorted(unknown))}")

    events = [_build_event(e, ctx) for e in raw.pop("events", []) or []]

    fields = dict(LEAD_DEFAULTS)
    fields.update(raw)
    for field in DATE_FIELDS:
        fields[field] = _coerce_date(fields.get(field), f"{ctx}.{field}")

    lead = SimpleNamespace(**fields)
    lead.events = _EventSet(events)
    return lead


# ---------------------------------------------------------------------------
# golden dataset
# ---------------------------------------------------------------------------

def load_golden(path):
    """Read the JSONL golden set, validating each record."""
    if not path.exists():
        raise SystemExit(f"Golden dataset not found: {path}")
    records = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: invalid JSON: {exc}")
            _validate_record(rec, f"{path}:{lineno}")
            records.append(rec)
    if not records:
        raise SystemExit(f"Golden dataset is empty: {path}")
    return records


def _validate_record(rec, where):
    action = rec.get("expected_action")
    if action not in ACTION_TYPES:
        raise SystemExit(
            f"{where}: expected_action {action!r} not in {ACTION_TYPES}"
        )
    priority = rec.get("expected_priority")
    if priority not in VALID_PRIORITIES:
        raise SystemExit(
            f"{where}: expected_priority {priority!r} not in {VALID_PRIORITIES}"
        )
    # Surface lead-construction problems (typo'd fields, bad dates) up front.
    build_lead(rec)


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def evaluate(records):
    rows = []
    for rec in records:
        lead = build_lead(rec)
        predicted_action, _reason = outreach.determine_action(lead, today=TODAY)
        predicted_priority = outreach.determine_priority(lead, today=TODAY)
        rows.append({
            "id": rec.get("id"),
            "tags": rec.get("tags", []),
            "expected_action": rec["expected_action"],
            "predicted_action": predicted_action,
            "expected_priority": rec["expected_priority"],
            "predicted_priority": predicted_priority,
        })
    return rows


def _round(value):
    return None if value is None else round(value, ROUND_NDIGITS)


def per_action_metrics(rows):
    """Per-action precision/recall/f1/support (rounded to ROUND_NDIGITS).

    Precision is ``None`` when an action is never predicted (0/0); recall is
    defined whenever the golden set contains at least one example of it.
    """
    metrics = {}
    for label in ACTION_TYPES:
        tp = sum(1 for r in rows if r["expected_action"] == label and r["predicted_action"] == label)
        predicted = sum(1 for r in rows if r["predicted_action"] == label)
        support = sum(1 for r in rows if r["expected_action"] == label)
        precision = (tp / predicted) if predicted else None
        recall = (tp / support) if support else None
        if precision and recall:  # both defined and > 0
            f1 = 2 * precision * recall / (precision + recall)
        elif precision is None or recall is None:
            f1 = None
        else:
            f1 = 0.0
        metrics[label] = {
            "precision": _round(precision),
            "recall": _round(recall),
            "f1": _round(f1),
            "support": support,
            "predicted": predicted,
        }
    return metrics


def confusion_matrix(rows):
    mat = {e: {p: 0 for p in ACTION_TYPES} for e in ACTION_TYPES}
    for r in rows:
        mat[r["expected_action"]][r["predicted_action"]] += 1
    return mat


def accuracy(rows, key_expected, key_predicted):
    if not rows:
        return None
    hits = sum(1 for r in rows if r[key_expected] == r[key_predicted])
    return _round(hits / len(rows)), hits, len(rows)


def priority_confusion(rows):
    mat = {e: {p: 0 for p in VALID_PRIORITIES} for e in VALID_PRIORITIES}
    for r in rows:
        mat[r["expected_priority"]][r["predicted_priority"]] += 1
    return mat


def build_results(rows):
    action_acc, action_hits, n = accuracy(rows, "expected_action", "predicted_action")
    priority_acc, priority_hits, _ = accuracy(rows, "expected_priority", "predicted_priority")
    return {
        "today": TODAY.isoformat(),
        "n": n,
        "action_accuracy": action_acc,
        "action_hits": action_hits,
        "priority_accuracy": priority_acc,
        "priority_hits": priority_hits,
        "per_action": per_action_metrics(rows),
    }


# ---------------------------------------------------------------------------
# baseline + gate
# ---------------------------------------------------------------------------

def load_baseline(path):
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_baseline(path, results):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            "Baseline for evals/run_rules_eval.py (MUS-20). Ground-truth labels: "
            "values may be <1.0 where the rules are imperfect. Regenerate with "
            "`python evals/run_rules_eval.py --update-baseline` after an intended change."
        ),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "today": results["today"],
        "n": results["n"],
        "action_accuracy": results["action_accuracy"],
        "priority_accuracy": results["priority_accuracy"],
        "per_action": {
            label: {k: results["per_action"][label][k] for k in ("precision", "recall", "f1", "support")}
            for label in ACTION_TYPES
        },
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


def check_gate(results, baseline):
    """Fail if any action type's precision or recall dropped below baseline.

    Gate scope is action-type only (per MUS-20); priority is report-only.
    """
    failures = []
    base_actions = baseline.get("per_action", {})
    for label in ACTION_TYPES:
        base = base_actions.get(label, {})
        cur = results["per_action"][label]
        for metric in ("precision", "recall"):
            base_val = base.get(metric)
            cur_val = cur.get(metric)
            if base_val is None:
                continue  # nothing recorded to regress against
            if cur_val is None:
                # Precision only: action is no longer predicted at all. The
                # matching recall check already flags this as a 0.0 regression,
                # so skip here to avoid a confusing duplicate failure.
                continue
            if cur_val < base_val - EPSILON:
                failures.append((label, metric, base_val, cur_val))
    return failures


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def _fmt(value):
    return " n/a " if value is None else f"{value:.3f}"


def print_report(records, rows, results, baseline):
    out = print
    out()
    out(f"Rules regression eval  —  golden: {results['n']} leads, today={results['today']}")
    out("=" * 74)

    # Per-action score table.
    out()
    out("Per-action-type scores")
    out("-" * 74)
    out(f"{'action_type':<22}{'support':>8}{'pred':>6}{'precision':>11}{'recall':>9}{'f1':>8}")
    for label in ACTION_TYPES:
        m = results["per_action"][label]
        out(f"{label:<22}{m['support']:>8}{m['predicted']:>6}"
            f"{_fmt(m['precision']):>11}{_fmt(m['recall']):>9}{_fmt(m['f1']):>8}")
    out("-" * 74)
    out(f"action-type accuracy : {results['action_accuracy']:.3f}  "
        f"({results['action_hits']}/{results['n']})")
    out(f"priority accuracy    : {results['priority_accuracy']:.3f}  "
        f"({results['priority_hits']}/{results['n']})   [report-only, not gated]")

    _print_action_confusion(rows)
    _print_priority_confusion(rows)
    _print_mismatches(rows)
    _print_coverage(records, rows)


def _print_action_confusion(rows):
    mat = confusion_matrix(rows)
    codes = [ACTION_CODE[a] for a in ACTION_TYPES]
    corner = "exp\\pred"
    print()
    print("Action-type confusion matrix  (rows = expected, cols = predicted)")
    print("legend: " + ", ".join(f"{ACTION_CODE[a]}={a}" for a in ACTION_TYPES))
    header = f"{corner:<22}" + "".join(f"{c:>5}" for c in codes)
    print(header)
    for exp in ACTION_TYPES:
        cells = "".join(f"{mat[exp][pred]:>5}" for pred in ACTION_TYPES)
        print(f"{exp:<22}{cells}")


def _print_priority_confusion(rows):
    mat = priority_confusion(rows)
    print()
    print("Priority confusion matrix  (rows = expected, cols = predicted)  [report-only]")
    corner = "exp\\pred"
    print(f"{corner:<10}" + "".join(f"{p:>5}" for p in VALID_PRIORITIES))
    for exp in VALID_PRIORITIES:
        cells = "".join(f"{mat[exp][pred]:>5}" for pred in VALID_PRIORITIES)
        print(f"{exp:<10}{cells}")


def _print_mismatches(rows):
    misses = [r for r in rows if r["expected_action"] != r["predicted_action"]]
    if not misses:
        return
    print()
    print(f"Action-type mismatches ({len(misses)})")
    for r in misses:
        print(f"  {r['id']:<32} expected {r['expected_action']:<20} got {r['predicted_action']}")


def _print_coverage(records, rows):
    print()
    print("Coverage")
    print("-" * 74)
    counts = {label: sum(1 for r in rows if r["expected_action"] == label) for label in ACTION_TYPES}
    for label in ACTION_TYPES:
        flag = "" if counts[label] else "   <-- NO EXAMPLES"
        print(f"  {label:<22} {counts[label]:>3} labeled{flag}")
    boundary_tags = sorted({t for rec in records for t in rec.get("tags", []) if t.startswith("boundary:")})
    print(f"  boundary tags present ({len(boundary_tags)}): "
          + (", ".join(t.split(':', 1)[1] for t in boundary_tags) or "none"))
    if len(records) < 30:
        print(f"  WARNING: only {len(records)} leads (<30 requested by MUS-20)")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Rules regression eval for the lead classifier.")
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH, help="Path to the golden JSONL set.")
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH, help="Path to the baseline JSON.")
    parser.add_argument("--update-baseline", action="store_true",
                        help="Recompute and (re)write the baseline, then exit 0.")
    args = parser.parse_args(argv)

    records = load_golden(args.golden)
    rows = evaluate(records)
    results = build_results(rows)

    if args.update_baseline:
        old = load_baseline(args.baseline)
        write_baseline(args.baseline, results)
        print(f"Baseline written to {args.baseline}")
        _print_baseline_diff(old, results)
        return 0

    print_report(records, rows, results, load_baseline(args.baseline))

    baseline = load_baseline(args.baseline)
    print()
    if baseline is None:
        print(f"Gate: NO BASELINE at {args.baseline}. "
              f"Run `python evals/run_rules_eval.py --update-baseline` to record one.")
        return 1

    failures = check_gate(results, baseline)
    if failures:
        print(f"Gate: FAIL — {len(failures)} action metric(s) regressed below baseline:")
        for label, metric, base_val, cur_val in failures:
            print(f"  {label:<22} {metric:<10} baseline {base_val:.3f} -> now {cur_val:.3f}")
        print("If this change is intentional, rerun with --update-baseline.")
        return 1

    print("Gate: PASS — no action type regressed below baseline.")
    return 0


def _print_baseline_diff(old, results):
    if old is None:
        print("(no previous baseline; recorded fresh)")
        return
    old_actions = old.get("per_action", {})
    changed = False
    for label in ACTION_TYPES:
        for metric in ("precision", "recall", "f1"):
            before = old_actions.get(label, {}).get(metric)
            after = results["per_action"][label][metric]
            if before != after:
                changed = True
                print(f"  {label:<22} {metric:<10} {_fmt(before)} -> {_fmt(after)}")
    if not changed:
        print("(no change vs previous baseline)")


if __name__ == "__main__":
    sys.exit(main())
