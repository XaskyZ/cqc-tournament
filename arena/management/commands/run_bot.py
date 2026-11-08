import time

from django.core.management.base import BaseCommand, CommandError

from arena.telegram import CashierBot, TelegramError


class Command(BaseCommand):
    help = "Run Telegram sign-in, private cashier review and payment notification retries. Run one instance only."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Flush notifications and poll once.")

    def handle(self, *args, **options):
        try:
            bot = CashierBot()
            if not bot.cashier_id:
                raise CommandError("TELEGRAM_CASHIER_ID is not configured.")
            if bot.client.call("getWebhookInfo").get("url"):
                raise CommandError("Telegram webhook is active. Resolve it explicitly before polling.")
        except TelegramError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write("Telegram bot started; browser sign-in and private cashier review enabled.")
        while True:
            try:
                bot.flush_outbox()
                bot.poll_once()
            except TelegramError as exc:
                self.stderr.write(str(exc))
                if options["once"]:
                    raise CommandError("Telegram request failed; updates and notifications remain pending.") from None
                time.sleep(5)
            except KeyboardInterrupt:
                self.stdout.write("Cashier bot stopped.")
                break
            if options["once"]:
                break
