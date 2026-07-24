"""Set user experience mode (normal / power) from the command line."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from resume_app.experience import set_experience_mode
from resume_app.models import UserExperienceSettings

User = get_user_model()


class Command(BaseCommand):
    help = "Set experience mode for one or more users (normal or power)."

    def add_arguments(self, parser):
        parser.add_argument(
            "usernames",
            nargs="*",
            help="Usernames to update (omit when using --staff).",
        )
        parser.add_argument(
            "--mode",
            required=True,
            choices=["normal", "power"],
            help="Experience mode to apply.",
        )
        parser.add_argument(
            "--staff",
            action="store_true",
            help="Apply to all staff users.",
        )
        parser.add_argument(
            "--skip-onboarding",
            action="store_true",
            help="Mark onboarding complete when switching modes.",
        )

    def handle(self, *args, **options):
        mode = options["mode"]
        users = []
        if options["staff"]:
            users = list(User.objects.filter(is_staff=True))
        for username in options["usernames"]:
            try:
                users.append(User.objects.get(username=username))
            except User.DoesNotExist as exc:
                raise CommandError(f"User not found: {username}") from exc

        if not users:
            raise CommandError("Provide usernames or use --staff.")

        for user in users:
            set_experience_mode(user, mode)
            if options["skip_onboarding"]:
                exp = UserExperienceSettings.get_for_user(user)
                if not exp.onboarding_completed_at:
                    from django.utils import timezone

                    exp.onboarding_completed_at = timezone.now()
                    exp.save(update_fields=["onboarding_completed_at"])
            self.stdout.write(self.style.SUCCESS(f"{user.username}: {mode}"))
