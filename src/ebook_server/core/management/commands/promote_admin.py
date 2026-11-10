"""Create or update the local account for an LDAP uid and grant it the admin role."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from ...models import UserRole, canonical_ldap_uid, get_or_create_profile


class Command(BaseCommand):
    help = (
        "Grant the library admin role to an LDAP uid (the account is created if needed "
        "and takes effect on the user's next LDAP login)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("uid", help="LDAP uid of the user to promote.")
        parser.add_argument(
            "--staff",
            action="store_true",
            help="Also set is_staff so the user can sign in to /admin/.",
        )
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Also set is_superuser (implies --staff) for full /admin/ permissions.",
        )

    def handle(self, *args: object, **options: object) -> None:
        raw_uid = str(options["uid"])
        uid = canonical_ldap_uid(raw_uid)
        if not uid:
            raise CommandError("uid must not be empty")
        superuser = bool(options["superuser"])
        staff = bool(options["staff"]) or superuser
        with transaction.atomic():
            profile = get_or_create_profile(uid)
            user = get_user_model().objects.get(pk=profile.user_id)
            profile.role = UserRole.ADMIN
            profile.is_disabled = False
            profile.save(update_fields=["role", "is_disabled", "updated_at"])
            user.is_active = True
            if staff:
                user.is_staff = True
            if superuser:
                user.is_superuser = True
            user.save(update_fields=["is_active", "is_staff", "is_superuser"])
        self.stdout.write(
            f"promoted uid={profile.ldap_uid} role=admin "
            f"is_staff={user.is_staff} is_superuser={user.is_superuser}"
        )
