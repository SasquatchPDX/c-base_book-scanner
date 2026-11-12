"""Create a local account (or reset its password) for use while LDAP is turned off."""

from __future__ import annotations

import getpass
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from ...models import UserRole, canonical_ldap_uid, get_or_create_profile

MIN_PASSWORD_LENGTH = 8


class Command(BaseCommand):
    help = (
        "Create a local account or reset its password. Used with "
        "BOOK_SCANNER_LDAP_MODE=local. Prompts for the password unless "
        "--password-stdin is given."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("username", help="Login name (case-insensitive).")
        parser.add_argument(
            "--password-stdin",
            action="store_true",
            help="Read the password from the first line of stdin (for scripts).",
        )
        parser.add_argument(
            "--admin",
            action="store_true",
            help="Grant the library admin role plus /admin/ staff and superuser access.",
        )

    def handle(self, *args: object, **options: object) -> None:
        username = canonical_ldap_uid(str(options["username"]))
        if not username:
            raise CommandError("username must not be empty")
        password = self._read_password(bool(options["password_stdin"]))
        if len(password) < MIN_PASSWORD_LENGTH:
            raise CommandError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")

        with transaction.atomic():
            profile = get_or_create_profile(username)
            user = get_user_model().objects.get(pk=profile.user_id)
            user.set_password(password)
            user.is_active = True
            update_fields = ["password", "is_active"]
            profile_fields = ["is_disabled", "updated_at"]
            profile.is_disabled = False
            if options["admin"]:
                profile.role = UserRole.ADMIN
                profile_fields.append("role")
                user.is_staff = True
                user.is_superuser = True
                update_fields += ["is_staff", "is_superuser"]
            user.save(update_fields=update_fields)
            profile.save(update_fields=profile_fields)
        self.stdout.write(
            f"local password set for {user.username} role={profile.role} "
            f"is_staff={user.is_staff} is_superuser={user.is_superuser}"
        )

    def _read_password(self, from_stdin: bool) -> str:
        if from_stdin:
            return sys.stdin.readline().rstrip("\r\n")
        first = getpass.getpass("Password: ")
        second = getpass.getpass("Password (again): ")
        if first != second:
            raise CommandError("passwords do not match")
        return first
