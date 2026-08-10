"""Django bootstrap imported by production and integration RQ workers."""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ebook_server.settings")
django.setup()
