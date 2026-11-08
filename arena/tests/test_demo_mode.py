from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase, override_settings
from arena.forms import PaymentReviewForm
from arena.providers import ManualKaspiProvider
from arena.telegram import CashierBot


@override_settings(DEMO_MODE=True, TELEGRAM_CASHIER_ID=855341070)
class DemoModeTests(SimpleTestCase):
    def test_demo_cannot_create_real_kaspi_checkout(self):
        self.assertIsNone(ManualKaspiProvider().create_checkout(object()))

    def test_demo_queue_does_not_send_financial_messages(self):
        client = MagicMock()
        CashierBot(client).flush_outbox()
        client.call.assert_not_called()

    def test_demo_payment_callbacks_do_not_mutate_financial_records(self):
        client = MagicMock()
        bot = CashierBot(client)
        with patch('arena.telegram_auth.handle_auth_update', return_value=False), patch.object(bot, '_callback') as mutate:
            bot.handle_update({'callback_query': {'id': 'demo-query', 'data': 'pr:y:test'}})
        mutate.assert_not_called()
        self.assertEqual(client.call.call_args.args[0], 'answerCallbackQuery')

    def test_demo_review_does_not_request_bank_payment(self):
        form = PaymentReviewForm()
        self.assertEqual(form.fields['bank_reference'].label, 'Тестовый номер операции')
        self.assertNotIn('Kaspi', form.fields['verified'].label)
