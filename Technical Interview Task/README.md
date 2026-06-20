# Eventual - Agentic Outreach Planner

## Background

Eventual builds Premium Lock, a financial product that gives homeowners predictability on their insurance premiums, protecting against rate increases at renewal. We sell through independent insurance agencies.

We have a pipeline of agency leads that our account executives (AEs) work to convert. Every morning, an AE manually reviews HubSpot and a few Slack channels to figure out who to reach out to and what to say. It takes too long, it's inconsistent, and a lot of signals get missed.

## The Ask

Build a tool that looks at our pipeline data and tells our AEs who to reach out to today and what to say.

You have two data files:

- `leads.json` — the agency pipeline
- `events.json` — a log of things that have happened with each lead

We'll send you an Anthropic API key shortly. We use Claude in production for this kind of thing and would like you to use it here too.

The backend should ingest the data, decide who needs outreach today and why, and use Claude to generate suggested copy per lead. The frontend just needs to display the results. Think of it as a replacement for opening Postman. It doesn't need to be pretty.

You have 75 minutes.

## What we're not telling you

- What signals matter most
- How to prioritize
- How to structure the prompt
- What the UI needs to look like beyond readable

Use your judgment. You can and should ask us whatever questions you have, but be thoughtful about your questions. Our intention is to test your ability not just to build this, but to work out what it should be doing for us from a business perspective. Your opinions matter.

## Stack

We use React / Next.js on the frontend and FastAPI on the backend, but use whatever you're fastest in.

## AI Tools

We build with AI assistance and expect you to as well. It's part of how we work. We use Cursor and Claude Code; let us know which you prefer and we'll get you set up.
