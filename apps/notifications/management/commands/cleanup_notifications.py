from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.notifications.models import Notification


class Command(BaseCommand):
    """Purges notifications older than --days (default 30). Per Shereena's
    spec (2026-08-28): the notification screen shows today's by default and
    keeps older ones around for 7-30 days for anyone who explicitly asks for
    them (see NotificationListView's ?all=true/?date=), but nothing should
    accumulate forever — run this periodically (e.g. a daily Render cron
    job) rather than on every request.
    """

    help = "Delete notifications older than N days (default 30)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(days=options["days"])
        deleted_count, _ = Notification.objects.filter(created_at__lt=cutoff).delete()
        self.stdout.write(f"Deleted {deleted_count} notification(s) older than {options['days']} day(s).")
