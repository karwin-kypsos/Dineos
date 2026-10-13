import os

from django.core.management.base import BaseCommand, CommandError

from apps.platform.models import PlatformAdmin


class Command(BaseCommand):
    help = (
        "Create (or update the password of) the real platform Super Admin "
        "account, reading credentials from SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD "
        "in .env — never pass the password on the command line or in chat."
    )

    def handle(self, *args, **options):
        email = os.environ.get("SUPERADMIN_EMAIL")
        password = os.environ.get("SUPERADMIN_PASSWORD")

        if not email or not password:
            raise CommandError(
                "Set SUPERADMIN_EMAIL and SUPERADMIN_PASSWORD in .env first, then re-run this command."
            )

        from django.utils import timezone

        admin, created = PlatformAdmin.objects.get_or_create(email=email, defaults={"name": "Super Admin"})
        admin.set_password(password)
        admin.is_active = True
        admin.access_level = PlatformAdmin.AccessLevel.FULL_ADMIN
        admin.must_change_password = False
        if not created:
            # The break-glass reset: sessions opened with the old password end.
            admin.password_changed_at = timezone.now()
        admin.save()
        if not created:
            admin.end_sessions()

        if created:
            self.stdout.write(self.style.SUCCESS(f"Created Super Admin: {email}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Updated password for existing Super Admin: {email}"))
