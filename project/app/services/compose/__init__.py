"""Run Composer (MUS-47): scoped runs, an advisory agent read, selective generation.

Four stages, each separately consented to by the person paying:

  01 scope     filter the lead table                      free (SQL)
  02 classify  deterministic rules over everyone in scope free, instant
  03 read      OPTIONAL model pass proposing adjustments  cheap model, priced first
  04 generate  copy for the leads actually selected       the expensive one

The invariant that makes "agentic priority" coexist with "rules decide":
``RunLead.rules_priority`` is written once by stage 02 and never written again.
Everything the model contributes lands on ``effective_priority``, and only after a
human accepts it. See ``docs/contracts/run-composer.md``.
"""
