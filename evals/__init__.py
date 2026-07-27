"""Evaluation harnesses for the Outreach Planner.

- ``run_rules_eval.py`` — rules-classifier regression suite (MUS-20).
- ``run_copy_eval.py`` / ``copy_eval.py`` — LLM-judge copy quality eval (MUS-21).
- ``copy_checks.py`` — pure-Python deterministic copy checks (no LLM, no deps),
  importable by the Django test suite.

Making ``evals`` a package lets the copy harness reuse the rules harness's
golden loader (``load_golden`` / ``build_lead``) instead of duplicating it.
"""
