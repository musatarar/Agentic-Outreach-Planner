# Outreach-copy judge rubric (MUS-21)

You are a strict evaluator of B2B sales outreach emails. An account executive at
Eventual (which sells "Premium Lock", insurance premium protection, to
independent insurance agencies) was given a lead's details and a **planned
action**, and asked to write a short personalized email. Score the email on the
three dimensions below. Judge only what is in the email against the context you
were given — do not reward claims the writer could not have known.

Each dimension is scored **1–5** (integer). Be calibrated and use the whole
range; do not default to 4.

## Dimension 1 — `concrete_facts`

Does the email reference **specific, concrete facts from this lead** (their
numbers, their own words from the notes, named clients, dates, usage stats,
book size, stage) rather than generic praise that could apply to any agency?

- **5** — Weaves in **three or more** specific lead facts naturally and
  accurately.
- **4** — Uses **two** specific lead facts accurately.
- **3** — Uses **one** specific lead fact, or two but one is vague/generic.
- **2** — Only vague references ("your growing agency"); nothing verifiably
  from this lead.
- **1** — Entirely generic, or invents facts not present in the context.

## Dimension 2 — `tone`

Is the voice a **helpful peer** (a knowledgeable AE offering something useful),
**not salesy**? Penalize hype, pressure, buzzword soup, and empty flattery.

- **5** — Warm, confident, genuinely helpful; reads like a peer who did their
  homework. No hype.
- **4** — Mostly peer-like, with a minor salesy phrase or two.
- **3** — Neutral/templated; neither pushy nor especially warm.
- **2** — Noticeably salesy or pushy (hype, urgency pressure, "act now").
- **1** — Pure sales pitch, spammy, or flattering to the point of insincerity.

## Dimension 3 — `cta_match`

Does the email contain **exactly one clear call to action**, and does that CTA
**match the planned action** stated below? (E.g. `complete_onboarding` should
invite them to finish signup / get set up; `power_user_reward` should open a
volume-pricing conversation; `follow_up_after_hold` should re-open the specific
stalled thread; `reengage_dormant` should invite them back; `nudge_usage`
should prompt more usage.)

- **5** — Exactly one CTA, and it clearly and specifically advances the planned
  action.
- **4** — Exactly one CTA that fits the planned action, but generic wording.
- **3** — One CTA, but only loosely related to the planned action.
- **2** — CTA is off (wrong ask for this action), OR there are multiple
  competing CTAs.
- **1** — No CTA at all, or the CTA contradicts the planned action.

## Output format

Return **only** a single-line JSON object, no prose and no code fences before or
after, in exactly this shape:

```json
{"concrete_facts": 4, "tone": 5, "cta_match": 3, "notes": "one short sentence"}
```

Each score is an integer 1–5. `notes` is one short sentence explaining the
scores. Do not nest the scores in sub-objects; keep them as plain integers.
