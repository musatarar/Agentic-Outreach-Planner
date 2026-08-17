"""
URL configuration for project project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import include, path

from project.app.views_frontend import (
    auth_consume,
    done,
    inbox,
    index,
    leads,
    next_actions,
    reports,
    settings,
    signin,
)

# There is no SPA catch-all: every React route in frontend/src/main.tsx needs a
# matching entry below or a hard refresh 404s. Under `npm run dev` Vite serves
# any path, so a missing route here only shows up against the built bundle.
# The trailing slashes are asymmetric on purpose: /signin,
# /auth/consume, /inbox and /done have none; the legacy four keep theirs.
urlpatterns = [
    path("", index),
    path("signin", signin),
    path("auth/consume", auth_consume),
    path("inbox", inbox),
    path("done", done),
    path("leads/", leads),
    path("reports/", reports),
    path("next-actions/", next_actions),
    path("settings/", settings),
    path("admin/", admin.site.urls),
    path("api/", include("project.app.urls")),
]
