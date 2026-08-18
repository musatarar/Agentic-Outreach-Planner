from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def index(request):
    """Render the Outreach Planner frontend page."""
    return render(request, "app/index.html")


@ensure_csrf_cookie
def reports(request):
    """Render the Outreach Reports page (per-lead audit trail)."""
    return render(request, "app/reports.html")


@ensure_csrf_cookie
def next_actions(request):
    """Render the BD Dashboard (manual review queue)."""
    return render(request, "app/next_actions.html")


@ensure_csrf_cookie
def leads(request):
    """Render the leads table shell — the page signing in lands on.

    Public like every other shell: it renders an empty #root and holds no data,
    and access control is the client-side guard in RequireAuth.tsx.
    """
    return render(request, "app/leads.html")


@ensure_csrf_cookie
def settings(request):
    """Render the Settings page (LLM provider/model/API key selection)."""
    return render(request, "app/settings.html")


@ensure_csrf_cookie
def signin(request):
    """Render the sign-in page (magic-link request). Public: no data on it."""
    return render(request, "app/signin.html")


@ensure_csrf_cookie
def auth_consume(request):
    """Render the token-consumption page. Public; the token is in the query string."""
    return render(request, "app/auth_consume.html")


@ensure_csrf_cookie
def inbox(request):
    """Render the triage inbox shell. Access control is the client-side guard."""
    return render(request, "app/inbox.html")


@ensure_csrf_cookie
def done(request):
    """Render the 'done today' shell. Access control is the client-side guard."""
    return render(request, "app/done.html")
