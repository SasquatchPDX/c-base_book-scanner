"""ASGI config for ebook_server."""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ebook_server.settings")

application = get_asgi_application()
