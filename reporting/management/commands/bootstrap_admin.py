import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or update the single local operator account from environment variables."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=os.environ.get("ADMIN_USERNAME", "admin"))
        parser.add_argument("--email", default=os.environ.get("ADMIN_EMAIL", "admin@ultex.local"))
        parser.add_argument("--password", default=os.environ.get("ADMIN_PASSWORD", ""))

    def handle(self, *args, **options):
        password = options["password"]
        if not password:
            raise CommandError("Set ADMIN_PASSWORD or pass --password.")
        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(username=options["username"], defaults={"email": options["email"]})
        user.email = options["email"]
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS(f"Operator account {'created' if created else 'updated'}."))

