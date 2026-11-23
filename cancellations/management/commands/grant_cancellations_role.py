from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from cancellations.permissions import ROLE_GROUP


class Command(BaseCommand):
    help = "Add a user to the 'cancellations' role (the only role that can create, send and review cases)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--revoke", action="store_true", help="Remove the role instead.")

    def handle(self, username, revoke=False, **options):
        try:
            user = get_user_model().objects.get(username=username)
        except get_user_model().DoesNotExist:
            raise CommandError(f"No user named {username}")
        group, _ = Group.objects.get_or_create(name=ROLE_GROUP)
        if revoke:
            user.groups.remove(group)
            self.stdout.write(self.style.SUCCESS(f"Removed {username} from {ROLE_GROUP}"))
        else:
            user.groups.add(group)
            self.stdout.write(self.style.SUCCESS(f"Added {username} to {ROLE_GROUP}"))
