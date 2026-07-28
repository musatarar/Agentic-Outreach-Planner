from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def index(request):
    """Render the Outreach Planner frontend page."""
    return render(request, "app/index.html")


@ensure_csrf_cookie
def reports(request):
    """Render the Outreach Reports page (per-lead audit trail).

    Decorated (like the other two shells) so the csrftoken cookie is set even
    when Reports is the first page loaded: the SPA may then client-side navigate
    to Planner/Dashboard and POST without a full reload that would set it.
    """
    return render(request, "app/reports.html")


@ensure_csrf_cookie
def next_actions(request):
    """Render the BD Dashboard (manual review queue)."""
    return render(request, "app/next_actions.html")


@ensure_csrf_cookie
def settings(request):
    """Render the Settings page (LLM provider/model/API key selection)."""
    return render(request, "app/settings.html")
