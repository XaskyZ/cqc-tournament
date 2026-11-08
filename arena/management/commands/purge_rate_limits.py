from django.core.management.base import BaseCommand

from arena.rate_limits import purge_expired


class Command(BaseCommand):
    help = "Delete up to 1000 expired request-limit buckets (safe to run periodically)."

    def handle(self, *args, **options):
        self.stdout.write(f"Expired rate-limit buckets removed: {purge_expired()}")
