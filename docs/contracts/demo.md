# Contract — demo

Throwaway rehearsal contract for the MUS-53 gate playbook: three tiny components wired by
an assembly, pushed through every gate class for real. Removed again by the cleanup PR.

## Interfaces

- `project/app/services/demo_alpha.py` — `normalize_lead_name(raw: str) -> str`:
  collapses runs of whitespace and title-cases an agency name.
- `project/app/services/demo_beta.py` — `score_lead(quotes_created: int, deals_closed: int) -> int`:
  returns `quotes_created + 3 * deals_closed`.
- `project/app/services/demo_assembly.py` —
  `demo_summary(raw_name: str, quotes_created: int, deals_closed: int) -> dict`:
  composes the two components into `{"name": ..., "score": ...}`.

## Data shapes

Plain builtins: `str` and `int` in, `dict[str, str | int]` out of the assembly.

## Error contract

Unimplemented stubs raise `NotImplementedError`; implemented functions raise nothing on
the inputs above.

## File map

```yaml
# file-map
feature: demo
components:
  alpha:
    files:
      - project/app/services/demo_alpha.py
    tests: project/app/tests_demo_alpha.py
  beta:
    files:
      - project/app/services/demo_beta.py
    tests: project/app/tests_demo_beta.py
  assembly:
    files:
      - project/app/services/demo_assembly.py
    tests: project/app/tests_demo_assembly.py
shared: []
```
