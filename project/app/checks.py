"""Django system checks for project.app.

Registered from AppConfig.ready() so a misconfigured deploy fails at boot,
not at first LLM call.
"""

from django.core.checks import Error, register
from django.db import connections


@register()
def planner_runtime_check(app_configs, **kwargs):
    """Range-check the MUS-26 planner knobs at boot, not at first run.

    Calls the same accessor the planner calls, so check and run cannot disagree.
    """
    from django.core.exceptions import ImproperlyConfigured

    from project.app.services.llm import runtime

    try:
        runtime.get_planner_runtime()
    except ImproperlyConfigured as exc:
        return [Error(str(exc), id="app.E002")]
    return []


@register()
def bulk_create_pk_check(app_configs, **kwargs):
    """The planner's phase 5 needs ``bulk_create`` to return primary keys.

    Postgres always does; SQLite only from 3.35. Feature detection only -- no
    query, so it is safe before migrations.
    """
    if connections["default"].features.can_return_rows_from_bulk_insert:
        return []
    return [
        Error(
            "This database cannot return primary keys from bulk_create "
            "(SQLite >= 3.35 or Postgres is required). The planner's batched "
            "insert would produce rows the API serializes with a null id.",
            id="app.E003",
        )
    ]
