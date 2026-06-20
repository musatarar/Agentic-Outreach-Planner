from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def index(request):
    """Render the Outreach Planner frontend page."""
    return render(request, 'app/index.html')


def reports(request):
    """Render the Outreach Reports page (per-lead audit trail)."""
    return render(request, 'app/reports.html')


def next_actions(request):
    """Render the stubbed Next Actions page (open follow-up queue)."""
    return render(request, 'app/next_actions.html')
