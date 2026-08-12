"""Versioned static Sure Lock product facts (MUS-29).

Backing data for the ``get_product_details`` tool. Deliberately a constant, not
a table: the facts restate only what ``_build_copy_prompt`` already asserts
about the product, so the deterministic verifier's grounding stays consistent
with what the tool tells the model.

Skeleton placeholder — the ``agent_tools`` component PR populates it and bumps
``version`` to 1.
"""

from __future__ import annotations

from typing import Any

PRODUCT_CATALOG: dict[str, Any] = {"version": 0}
