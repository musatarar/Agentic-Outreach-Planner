"""Versioned static Sure Lock product facts (MUS-29).

Backing data for the ``get_product_details`` tool. A constant rather than a
table, so it restates only what ``_build_copy_prompt`` already asserts and the
verifier's grounding stays consistent with what the tool tells the model.
"""

from __future__ import annotations

from typing import Any

PRODUCT_CATALOG: dict[str, Any] = {
    "version": 1,
    "product": "Sure Lock",
    "company": "Locked In",
    "category": "insurance premium protection for homeowners",
    "distribution": "sold through independent insurance agencies",
}
