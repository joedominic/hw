"""Grant or revoke Django staff / Support-console access for users."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError

User = get_user_model()

SUPPORT_GROUP_NAME = "Support"
IMPERSONATE_PERM = "can_impersonate_users"


def ensure_support_group() -> Group:
    """Create the Support group and impersonation permission if missing."""
    ct, _ = ContentType.objects.get_or_create(
        app_label="resume_app",
        model="impersonationauditlog",
    )
    perm, _ = Permission.objects.get_or_create(
        codename=IMPERSONATE_PERM,
        content_type=ct,
        defaults={"name": "Can impersonate users for support"},
    )
    group, _ = Group.objects.get_or_create(name=SUPPORT_GROUP_NAME)
    group.permissions.add(perm)
    return group


class Command(BaseCommand):
    help = (
        "Mark users as Django staff and (by default) add them to the Support group "
        "so they can use /staff/users/ and /admin/."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "usernames",
            nargs="+",
            help="Usernames to promote or demote.",
        )
        parser.add_argument(
            "--remove",
            action="store_true",
            help="Revoke staff status, Support group, and (unless kept) superuser.",
        )
        parser.add_argument(
            "--no-support",
            action="store_true",
            help="Only set is_staff; do not add/remove the Support group.",
        )
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Also set is_superuser (ignored with --remove unless --keep-superuser is unset).",
        )
        parser.add_argument(
            "--keep-superuser",
            action="store_true",
            help="With --remove, leave is_superuser unchanged.",
        )

    def handle(self, *args, **options):
        usernames = options["usernames"]
        remove = options["remove"]
        no_support = options["no_support"]
        make_super = options["superuser"]
        keep_super = options["keep_superuser"]

        support_group = None if no_support else ensure_support_group()

        for username in usernames:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist as exc:
                raise CommandError(f"User not found: {username}") from exc

            if remove:
                self._revoke(user, support_group=support_group, keep_super=keep_super)
            else:
                self._grant(
                    user,
                    support_group=support_group,
                    make_super=make_super,
                )

    def _grant(self, user, *, support_group: Group | None, make_super: bool) -> None:
        update_fields: list[str] = []
        if not user.is_staff:
            user.is_staff = True
            update_fields.append("is_staff")
        if make_super and not user.is_superuser:
            user.is_superuser = True
            update_fields.append("is_superuser")
        if update_fields:
            user.save(update_fields=update_fields)

        parts = ["is_staff=True"]
        if support_group is not None:
            support_group.user_set.add(user)
            parts.append(f"group={SUPPORT_GROUP_NAME}")
        if user.is_superuser:
            parts.append("is_superuser=True")

        self.stdout.write(self.style.SUCCESS(f"{user.username}: " + ", ".join(parts)))

    def _revoke(self, user, *, support_group: Group | None, keep_super: bool) -> None:
        update_fields: list[str] = []
        if user.is_staff:
            user.is_staff = False
            update_fields.append("is_staff")
        if not keep_super and user.is_superuser:
            user.is_superuser = False
            update_fields.append("is_superuser")
        if update_fields:
            user.save(update_fields=update_fields)

        parts = ["is_staff=False"]
        if support_group is not None:
            support_group.user_set.remove(user)
            parts.append(f"removed group={SUPPORT_GROUP_NAME}")
        if not keep_super:
            parts.append(f"is_superuser={user.is_superuser}")

        self.stdout.write(self.style.WARNING(f"{user.username}: " + ", ".join(parts)))
