# Contract — <feature>

Copy this file to `docs/contracts/<feature>.md` (the `<feature>` segment of your
`feat/<feature>` branch, verbatim) and fill in every section. The workflow gate lints this
document (`python scripts/check_scope.py --validate-contract docs/contracts/<feature>.md`):
the four section headings below are required, and the `# file-map` block must parse and
satisfy the ownership rules. Lint checks section *presence* — making the prose precise
enough that components built against it in isolation actually compose is review-only, and
it is the part that matters most.

A consistent wrong contract merges; N locally-correct contracts do not. Once the skeleton
PR lands, this document is frozen — changes go through a `feat/<feature>--contract*` PR
that touches only this file and test expectations.

## Interfaces

Every function/class/endpoint each component exposes, with exact names, signatures, and
module paths. Other components call these blind; if it isn't written here, it doesn't
exist.

```python
# example
def compose_run(leads: list[Lead], *, today: date) -> RunPlan: ...
```

## Data shapes

The exact shapes crossing component boundaries (dataclasses, TypedDicts, JSON payloads),
including field names, types, optionality, and units.

## Error contract

What each interface raises or returns on failure. Unimplemented skeleton stubs raise
`NotImplementedError` — the red-proof gate counts that (or an assertion mismatch against
contract output) as failing for the right reason.

## File map

Machine-readable ownership. Every owned file belongs to exactly one component; each
component declares at least one Django test module (`project/app/tests*.py`); an
`assembly` component must exist; `shared` files are written in the skeleton and owned by
no component — mini PRs may not touch them. No protected path may appear anywhere here.

```yaml
# file-map
feature: run-composer
components:
  scope_engine:
    files:
      - project/app/services/scope.py
    tests: project/app/tests_scope_engine.py
  assembly:
    files: []
    tests: project/app/tests_run_composer_assembly.py
shared:
  - project/app/urls.py
  - frontend/src/api/types.ts
```
