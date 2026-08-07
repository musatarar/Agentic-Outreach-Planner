#!/usr/bin/env python3
"""Wall-clock benchmark for the concurrent planner (MUS-26e).

Measures ``plan_outreach()`` end to end against the stub provider, at a given
concurrency, on a given number of synthetic leads. Everything real runs: the
rules, the prompt building, the semaphore, the retry loop, both output gates,
and the bulk write. Only the provider is fake, and it is fake in a way that
still returns a well-formed, grounded email so the gates do real work (see
``project/app/services/llm/stub.py``).

Usage::

    python evals/bench_planner.py --leads 200 --concurrency 8
    python evals/bench_planner.py --leads 200 --concurrency 1   # the "before"
    python evals/bench_planner.py --table                       # render from results

**`--concurrency 1` is the honest "before" number.** It is a semaphore of one,
not a genuinely serial implementation -- and that is fine, because the stub
sleeps ~1.9s per call over 200 calls, so provider time dominates the difference
between the two by three orders of magnitude. Maintaining a second, serial
planner just to make a table look rigorous would mean shipping a code path
nobody runs, and the honest thing is to say which number is which. The N+1 fix
is reported separately, as an archaeological measurement, for the same reason:
a ``--no-prefetch`` flag kept alive in production code to make a benchmark
prettier is a worse trade than a sentence in the README.

**It never touches the dev database.** ``DATABASE_URL`` is pointed at a fresh
temporary SQLite file *before* ``django.setup()``, migrated, seeded, measured
and deleted. The twelve-lead demo pipeline is the DEMO.md contract, and a
benchmark that dropped two hundred synthetic rows into it would break the demo
for the sake of a number.
"""

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RESULTS_DIR = REPO_ROOT / "evals" / "results"
RESULT_GLOB = "bench-planner-*.json"

# Markers in README.md between which --table writes. Same idiom
# run_copy_eval.py --table already uses.
TABLE_START = "<!-- PLANNER-BENCH-TABLE -->"
TABLE_END = "<!-- /PLANNER-BENCH-TABLE -->"


def bootstrap_django(db_path):
    """Point Django at a throwaway SQLite file, then set it up.

    Order matters and is the whole safety property: ``DATABASE_URL`` is read by
    ``project/settings.py`` at import time, so it has to be in the environment
    *before* ``django.setup()`` -- after that, the connection is already
    configured and any later assignment is decoration.
    """
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.setdefault("DJANGO_SECRET_KEY", "benchmark-only-not-a-real-key")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")
    # The stub provider refuses to be constructed without this. Set here and
    # nowhere else in the repo.
    os.environ["OUTREACH_ALLOW_STUB_LLM"] = "1"

    import django

    django.setup()

    from django.conf import settings

    configured = settings.DATABASES["default"]["NAME"]
    if str(configured) != str(db_path):
        # Belt and braces. If a settings module ever stops honouring
        # DATABASE_URL, this benchmark must refuse to run rather than seed two
        # hundred synthetic leads into whatever database it did get.
        raise SystemExit(
            f"Refusing to run: expected the temporary database {db_path}, "
            f"but Django is configured for {configured}."
        )


def prepare(db_path, leads, seed):
    from django.core.management import call_command

    call_command("migrate", run_syncdb=True, verbosity=0)
    call_command("seed_synthetic_leads", count=leads, seed=seed, verbosity=0)


def run_once(concurrency, request_timeout_s, per_lead_timeout_s, stub_kwargs):
    """One measured ``plan_outreach()``, with the stub injected.

    The stub is injected by patching ``get_llm_client`` rather than by writing a
    "stub" row into ``LLMConfiguration``. That keeps the benchmark from ever
    creating a database state the app could accidentally inherit, and means the
    provider registry's only "stub" consumer is ``build_client("stub")``.
    """
    from unittest.mock import patch

    from django.test import override_settings

    from project.app.models import OutreachAction
    from project.app.services import outreach
    from project.app.services.llm import build_client
    from project.app.services.llm.stub import StubClient

    client = build_client("stub")
    assert isinstance(client, StubClient)  # noqa: S101 - the injection is the point
    for name, value in stub_kwargs.items():
        setattr(client, name, value)

    # A previous run leaves `pending` rows, which suppress a re-plan. Cleared
    # rather than worked around, so every measured run does the same amount of
    # work.
    OutreachAction.objects.all().delete()

    with override_settings(
        OUTREACH_MAX_IN_FLIGHT=concurrency,
        OUTREACH_REQUEST_TIMEOUT_S=request_timeout_s,
        OUTREACH_PER_LEAD_TIMEOUT_S=per_lead_timeout_s,
        # The stub's simulated 429s carry Retry-After: 0.05, and the schedule is
        # left at its defaults otherwise -- a benchmark that switched retries
        # off would not be measuring the planner that ships.
        COPY_VERIFY_LEVEL="standard",
    ):
        with patch.object(outreach, "get_llm_client", return_value=client):
            started = time.perf_counter()
            planned = outreach.plan_outreach()
            elapsed_s = time.perf_counter() - started

    return {
        "elapsed_s": elapsed_s,
        "planned": len(planned),
        "with_copy": sum(1 for action in planned if action.suggested_copy),
        "needs_human": sum(1 for action in planned if action.needs_human),
        "provider_calls": client.calls,
    }


def measure(args):
    stub_kwargs = {
        "latency_mean_s": args.latency,
        "latency_stddev_s": args.latency_stddev,
        "rate_limit_rate": args.rate_limit_rate,
        "failure_rate": args.failure_rate,
    }
    runs = []
    for index in range(args.repeat):
        result = run_once(
            args.concurrency, args.request_timeout, args.per_lead_timeout, stub_kwargs
        )
        runs.append(result)
        print(
            f"  run {index + 1}/{args.repeat}: {result['elapsed_s']:.2f}s  "
            f"({result['planned']} planned, {result['with_copy']} with copy, "
            f"{result['provider_calls']} provider calls)"
        )

    elapsed = [run["elapsed_s"] for run in runs]
    last = runs[-1]
    return {
        "leads": args.leads,
        "concurrency": args.concurrency,
        "repeat": args.repeat,
        "seed": args.seed,
        "latency_mean_s": args.latency,
        "rate_limit_rate": args.rate_limit_rate,
        "failure_rate": args.failure_rate,
        "elapsed_s_median": round(statistics.median(elapsed), 3),
        "elapsed_s_min": round(min(elapsed), 3),
        "elapsed_s_max": round(max(elapsed), 3),
        "planned": last["planned"],
        "with_copy": last["with_copy"],
        "needs_human": last["needs_human"],
        "provider_calls": last["provider_calls"],
        # The number that makes the table interpretable: sequential provider
        # time is what the run would have cost with no concurrency at all, so
        # elapsed / this is the speedup, and it is derived from the same run
        # rather than from a separate serial implementation.
        "sequential_provider_s": round(last["provider_calls"] * args.latency, 2),
    }


def write_result(result, results_dir=RESULTS_DIR):
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"bench-planner-c{result['concurrency']:03d}-n{result['leads']}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path


def render_table(results_dir=RESULTS_DIR):
    artifacts = sorted(results_dir.glob(RESULT_GLOB)) if results_dir.exists() else []
    if not artifacts:
        return "_No planner benchmark results yet. Run `python evals/bench_planner.py` first._"
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in artifacts]
    rows.sort(key=lambda row: (row["leads"], row["concurrency"]))

    baseline = {row["leads"]: row for row in rows if row["concurrency"] == 1}
    lines = [
        "| Leads | Concurrency | Wall clock (median) | Provider calls | Speedup |",
        "|--:|--:|--:|--:|--:|",
    ]
    for row in rows:
        base = baseline.get(row["leads"])
        speedup = (
            f"{base['elapsed_s_median'] / row['elapsed_s_median']:.1f}x"
            if base and row["elapsed_s_median"] > 0
            else "—"
        )
        lines.append(
            f"| {row['leads']} | {row['concurrency']} | "
            f"{row['elapsed_s_median']:.1f}s | {row['provider_calls']} | {speedup} |"
        )
    return "\n".join(lines)


def update_readme(table):
    readme = REPO_ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    if TABLE_START not in text or TABLE_END not in text:
        raise SystemExit(f"README.md is missing the {TABLE_START} / {TABLE_END} markers.")
    head, _, rest = text.partition(TABLE_START)
    _, _, tail = rest.partition(TABLE_END)
    readme.write_text(f"{head}{TABLE_START}\n{table}\n{TABLE_END}{tail}", encoding="utf-8")
    return readme


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--leads", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--repeat", type=int, default=1, help="Measured runs; median reported.")
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument(
        "--latency", type=float, default=1.87, help="Mean simulated provider latency, seconds."
    )
    parser.add_argument("--latency-stddev", type=float, default=0.45)
    parser.add_argument("--rate-limit-rate", type=float, default=0.0)
    parser.add_argument("--failure-rate", type=float, default=0.0)
    parser.add_argument("--request-timeout", type=float, default=60.0)
    parser.add_argument("--per-lead-timeout", type=float, default=150.0)
    parser.add_argument(
        "--table", action="store_true", help="Render the README table from existing results."
    )
    parser.add_argument(
        "--update-readme", action="store_true", help="Write the table into README.md in place."
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=RESULTS_DIR,
        help="Where result JSONs are written and read. The default is the committed "
        "table's source of truth; the test suite's smoke run points this at a "
        "temporary directory so a 6-lead artifact can never sit beside the real "
        "numbers waiting for the next --update-readme.",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.table or args.update_readme:
        table = render_table(args.results_dir)
        if args.update_readme:
            print(f"Wrote {update_readme(table)}")
        else:
            print(table)
        return 0

    # NamedTemporaryFile would hold an open handle SQLite does not want; a
    # directory that is removed wholesale is simpler and leaves nothing behind
    # even if the run raises.
    with tempfile.TemporaryDirectory(prefix="bench-planner-") as tmpdir:
        db_path = Path(tmpdir) / "bench.sqlite3"
        bootstrap_django(db_path)
        print(f"Temporary database: {db_path}")
        prepare(db_path, args.leads, args.seed)

        print(f"Benchmarking {args.leads} leads at concurrency {args.concurrency}...")
        result = measure(args)

    path = write_result(result, args.results_dir)
    shown = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
    print()
    print(f"  wall clock (median) : {result['elapsed_s_median']:.2f}s")
    print(f"  provider calls      : {result['provider_calls']}")
    print(f"  sequential estimate : {result['sequential_provider_s']:.1f}s of provider time")
    print(f"  planned / with copy : {result['planned']} / {result['with_copy']}")
    print(f"  wrote               : {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
