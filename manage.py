#!/usr/bin/env python
"""Compatibility entrypoint for local Django management commands."""

from __future__ import annotations

import os

from ebook_server.main import main

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ebook_server.settings")
    main()
