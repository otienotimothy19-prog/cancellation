from django.core.management.base import BaseCommand

from cancellations.services.sla import check_sla


class Command(BaseCommand):
    help = "Send SLA reminders for unanswered requests and flag overdue ones STALE."

    def handle(self, **options):
        r = check_sla()
        self.stdout.write(f"reminded={r.reminded} stale={r.flagged_stale} no_recipient={r.no_recipient}")
