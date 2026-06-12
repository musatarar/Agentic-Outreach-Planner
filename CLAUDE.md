# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Context

This is a starter scaffold for a 75-minute technical challenge: a bare Django 4.2 project (Python 3.9, SQLite) with Django REST Framework installed. Prioritize speed and working features over polish — avoid refactoring the scaffold, adding tooling, or gold-plating.

## Commands

All commands use the bundled virtualenv at `./venv` (no requirements.txt exists; key packages: Django 4.2.30, djangorestframework 3.16.1, django-excel-response2/xlwt for Excel export).

```bash
./venv/bin/python manage.py runserver          # run dev server (http://127.0.0.1:8000)
./venv/bin/python manage.py makemigrations app # create migrations after model changes
./venv/bin/python manage.py migrate            # apply migrations
./venv/bin/python manage.py test project.app   # run tests
./venv/bin/python manage.py test project.app.tests.SomeTestCase.test_name  # single test
./venv/bin/python manage.py shell              # Django shell
```

## Architecture & current state

- `project/` is both the settings package (`project/settings.py`, `project/urls.py`) and the parent of the single app at `project/app/`.
- The app (`project/app/`) is **empty stubs only**: no models, views, serializers, app URLs, or migrations yet.
- The app is **NOT in `INSTALLED_APPS`** yet. To wire it up:
  1. Because the app lives at `project/app/` (not top-level `app/`), register it as `'project.app'` in `INSTALLED_APPS` and change `name = 'app'` to `name = 'project.app'` in `project/app/apps.py`. Registering plain `'app'` will fail with an import error.
  2. Add DRF routes by creating `project/app/urls.py` and including it from `project/urls.py` (currently only routes `admin/`).
- `rest_framework` is already in `INSTALLED_APPS` — build APIs with DRF (serializers + ViewSets/APIViews + router) rather than plain Django views.
- Database is SQLite at `./db.sqlite3`; default Django migrations have already been applied.

## Git Workflow

- **Feature branch**: `feature/75-min-challenge` — primary working branch for the challenge
- **PR strategy**: Create separate PRs for each logical piece of work, each with its own task/agent if needed. All PRs target `feature/75-min-challenge`, not `main`.
- **Final merge**: Once all work PRs are merged into `feature/75-min-challenge`, create a final PR to merge the entire feature into `main`.
- **Tickets**: Use Linear to track work and scope each PR to a ticket. Use the `to-issues` skill to convert plans into Linear issues.
