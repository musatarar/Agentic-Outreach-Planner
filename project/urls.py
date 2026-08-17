"""Root URL configuration: SPA page shells plus the `api/` include."""

from django.contrib import admin
from django.urls import include, path

from project.app.views_frontend import (
    auth_consume,
    done,
    inbox,
    index,
    next_actions,
    reports,
    settings,
    signin,
)

# No SPA catch-all: every React route in frontend/src/main.tsx needs an entry
# below or a hard refresh 404s. Trailing slashes are asymmetric on purpose --
# the newer routes have none, the legacy four keep theirs.
urlpatterns = [
    path("", index),
    path("signin", signin),
    path("auth/consume", auth_consume),
    path("inbox", inbox),
    path("done", done),
    path("reports/", reports),
    path("next-actions/", next_actions),
    path("settings/", settings),
    path("admin/", admin.site.urls),
    path("api/", include("project.app.urls")),
]
