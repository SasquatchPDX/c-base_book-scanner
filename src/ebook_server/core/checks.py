"""Django system checks for optional integrations."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, register

from . import llm


@register(Tags.security, deploy=True)
def check_llm_service_is_local(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """The LLM identification assist may only talk to a local/private inference server."""
    if not getattr(settings, "BOOK_SCANNER_LLM_ENABLED", False):
        return []
    errors: list[CheckMessage] = []
    problem = llm.local_service_url_problem(
        str(getattr(settings, "BOOK_SCANNER_LLM_SERVICE_URL", "")),
        allowed_hosts=getattr(settings, "BOOK_SCANNER_LLM_ALLOWED_HOSTS", []),
    )
    if problem is not None:
        errors.append(
            Error(
                problem,
                hint="Point BOOK_SCANNER_LLM_SERVICE_URL at the local AI server, or disable "
                "the assist with BOOK_SCANNER_LLM_ENABLED=false.",
                id="ebook_server.E101",
            ),
        )
    if not str(getattr(settings, "BOOK_SCANNER_LLM_MODEL", "")).strip():
        errors.append(
            Error(
                "BOOK_SCANNER_LLM_ENABLED is true but BOOK_SCANNER_LLM_MODEL is empty.",
                id="ebook_server.E102",
            ),
        )
    return errors
