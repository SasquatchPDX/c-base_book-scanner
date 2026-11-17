"""Manage scanner devices and their API keys for ``/api/scanner/v1/``.

    manage.py scanner_device create <scanner_id> [--name "libreflip at c-base"]
    manage.py scanner_device rotate <scanner_id>
    manage.py scanner_device revoke <scanner_id>
    manage.py scanner_device list

``create`` and ``rotate`` print the new key exactly once; only its SHA-256 is stored.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from ...models import ScannerDevice
from ...scanner_devices import ScannerDeviceError, create_device, revoke_device, rotate_key


def _when(value: Any) -> str:
    return value.isoformat().replace("+00:00", "Z") if value is not None else "-"


class Command(BaseCommand):
    help = "Create, rotate, revoke or list scanner devices for the scanner device API."

    def add_arguments(self, parser: CommandParser) -> None:
        subcommands = parser.add_subparsers(dest="action", required=True)
        create = subcommands.add_parser("create", help="Register a device and print its key.")
        create.add_argument("scanner_id", help="Device slug, for example libreflip-01.")
        create.add_argument("--name", default="", help="Human-readable device name.")
        rotate = subcommands.add_parser(
            "rotate",
            help="Issue a new key; the old key is invalid immediately. Re-activates a "
            "revoked device.",
        )
        rotate.add_argument("scanner_id")
        revoke = subcommands.add_parser("revoke", help="Disable a device's key.")
        revoke.add_argument("scanner_id")
        subcommands.add_parser("list", help="List devices (keys are never shown).")

    def handle(self, *args: Any, **options: Any) -> None:
        action = options["action"]
        try:
            if action == "create":
                device, key = create_device(options["scanner_id"], name=options["name"])
                self._print_key(device, key, "created")
            elif action == "rotate":
                device, key = rotate_key(options["scanner_id"])
                self._print_key(device, key, "rotated")
            elif action == "revoke":
                device = revoke_device(options["scanner_id"])
                self.stdout.write(f"revoked scanner_id={device.scanner_id}")
            else:
                self._list()
        except ScannerDeviceError as exc:
            raise CommandError(str(exc)) from exc
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

    def _print_key(self, device: ScannerDevice, key: str, verb: str) -> None:
        self.stdout.write(f"{verb} scanner_id={device.scanner_id}")
        self.stdout.write(
            "API key (shown only now; store it in the scanner's api_key_file, mode 0600):"
        )
        self.stdout.write(key)

    def _list(self) -> None:
        devices = ScannerDevice.objects.order_by("scanner_id")
        if not devices:
            self.stdout.write("no scanner devices")
            return
        for device in devices:
            self.stdout.write(
                "\t".join(
                    [
                        device.scanner_id,
                        "active" if device.is_active else "revoked",
                        f"name={device.name or '-'}",
                        f"created={_when(device.created_at)}",
                        f"rotated={_when(device.rotated_at)}",
                        f"last_seen={_when(device.last_seen_at)}",
                    ]
                )
            )
