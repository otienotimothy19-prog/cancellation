from django.core.management.base import BaseCommand

from cancellations.services import portal


class Command(BaseCommand):
    help = "Check confirmed cases against the Production Portal now (the worker also does this)."

    def handle(self, **options):
        if not portal.enabled():
            self.stdout.write("The Production Portal is not connected (PRODUCTION_PORTAL_URL / _TOKEN).")
            return
        self.stdout.write(str(portal.check_due(limit=500) or "Nothing due."))
