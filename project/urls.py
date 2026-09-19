"""Root URL configuration: SPA page shells plus the `api/` include."""

from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView

from project.app.views.frontend import (
    actions,
    auth_consume,
    inbox,
    leads,
    signin,
)

# No SPA catch-all: every React route in frontend/src/main.tsx needs an entry
# below or a hard refresh 404s. Trailing slashes are asymmetric on purpose --
# the newer routes have none, the legacy ones keep theirs. `/` is not a React
# route: it redirects to the book of leads, where signing in lands you.
urlpatterns = [
    path("", RedirectView.as_view(url="/leads/", permanent=False)),
    path("signin", signin),
    path("auth/consume", auth_consume),
    path("actions", actions),
    path("inbox", inbox),
    path("leads/", leads),
    path("admin/", admin.site.urls),
    path("api/", include("project.app.urls")),
]
