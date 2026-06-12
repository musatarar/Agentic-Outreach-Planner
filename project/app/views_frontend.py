from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def index(request):
    """Render the Outreach Planner frontend page."""
    return render(request, 'app/index.html')
