from django.core.management.base import BaseCommand

from arena.notifications import queue_readiness_reminders, send_pending_notifications


class Command(BaseCommand):
    help = "Queue due readiness reminders; optionally deliver event notifications to Telegram."

    def add_arguments(self, parser):
        parser.add_argument("--deliver", action="store_true", help="Requires EVENT_TELEGRAM_ENABLED=1 and non-demo mode.")
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        created = queue_readiness_reminders()
        self.stdout.write(f"Queued readiness notifications: {created}")
        if options["deliver"]:
            result = send_pending_notifications(limit=options["limit"])
            self.stdout.write(f"Sent: {result['sent']}; failed: {result['failed']}; delivery disabled: {result['disabled']}")
