"""Generate the scanner API key and print it with its settings line (#125).

The command writes nothing anywhere: the operator copies the key to the scanner and the
``BOOK_SCANNER_SCANNER_API_KEY_SHA256=<hex>`` line to the server environment, then
restarts the web service.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from ebook_server.core.scanner_auth import SETTING_NAME, generate_key, hash_key


class Command(BaseCommand):
    help = (
        "Generate a new scanner API key. Prints the key (for the scanner) and the "
        f"{SETTING_NAME} line (for the server environment); stores nothing."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        key = generate_key()
        self.stdout.write("Scanner API key (put it in the scanner's api_key_file; shown once):")
        self.stdout.write(key)
        self.stdout.write("")
        self.stdout.write("Add this line to the server environment and restart the web service:")
        self.stdout.write(f"{SETTING_NAME}={hash_key(key)}")
