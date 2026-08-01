# README images

## `mus-25-trace.png` — the acceptance evidence for MUS-25 and MUS-26

**Not committed yet.** It needs a real provider key and a genuinely rate-limited run, and a
mocked-up picture of a trace would be worth less than no picture: the whole claim it makes is
"this happened", and a synthesised one makes exactly the opposite claim while looking identical.

### What the shot has to show

One lead that got throttled, expanded:

- a `plan_lead` span with `outreach.needs_human = false` — the run *recovered*
- three `chat {model}` child spans beneath it
- the first two red, each with `error.type = LLMRateLimitError` and `outreach.llm.retry_after_s`
- the third green, with `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` populated
- the attribute pane open on one of the red ones, so `outreach.llm.attempt` is visible

That single image is the acceptance evidence for **both** tickets at once: MUS-25's claim that a
run produces a readable trace tree with token counts and per-lead latency, and MUS-26's claim that
a rate-limited call is retried rather than routed to a human on the first refusal.

### How to capture it

Groq's free tier is the easiest way to get real 429s — it throttles quickly and costs nothing.

```bash
# 1. A real key, and enough concurrency to provoke the free tier.
cp .env.example .env
$EDITOR .env            # set DJANGO_SECRET_KEY (one ships in the example) and GROQ_API_KEY

# 2. Bring up Postgres, Phoenix and the app. Tracing is already wired in
#    docker-compose.yml -- OTEL_EXPORTER_OTLP_ENDPOINT points at phoenix:6006.
docker compose up --build

# 3. Seed the pipeline and plan. 200 leads at concurrency 8 reliably trips the
#    free tier; a smaller run may not.
docker compose exec web python scripts/populate_demo_data.py
docker compose exec web python manage.py shell -c \
  "from project.app.services.outreach import plan_outreach; plan_outreach()"

# 4. Open Phoenix, find the run, expand a lead with three chat spans.
open http://localhost:6006
```

Then save the screenshot here as `mus-25-trace.png` and replace the "Screenshot pending" block in
`README.md` with:

```markdown
![Trace of a rate-limited outreach run](docs/img/mus-25-trace.png)
```

### If no lead gets throttled

Raise the concurrency, or lower Groq's ceiling by picking a larger model in
`/api/llm/config/`. Failing that, `OUTREACH_OTEL_CONSOLE_METRICS=1` will at least print the
duration and token histograms to the container log, which shows the same numbers without the
picture.

Do **not** simulate the 429s to get the shot. A trace of a stubbed failure is a screenshot of the
test suite, not of the product.
