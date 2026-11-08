"""Payment integration boundary. No undocumented Kaspi API or screen scraping.

A future official adapter must authenticate bank events and reconcile amount,
currency, recipient, immutable operation ID and participant before producing a
VerifiedPayment. The manual provider intentionally cannot do that.
"""
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from django.core.exceptions import ValidationError
from django.conf import settings


@dataclass(frozen=True)
class Checkout:
    url: str
    amount: int
    currency: str
    reference: str
    manual_review_required: bool


@dataclass(frozen=True)
class VerifiedPayment:
    provider: str
    operation_id: str
    amount: int
    currency: str
    reference: str


class PaymentProvider(Protocol):
    def create_checkout(self, payment) -> Checkout | None: ...
    def verify_event(self, body: bytes, headers: dict) -> VerifiedPayment: ...


class ManualKaspiProvider:
    def create_checkout(self, payment) -> Checkout | None:
        if settings.DEMO_MODE or not payment.participant.active or payment.participant.team.status in ('draft', 'rejected') or not payment.required:
            return None
        url = payment.participant.team.tournament.kaspi_url
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'pay.kaspi.kz' or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValidationError('Организатор должен указать корректную ссылку Kaspi Pay.')
        return Checkout(url=url, amount=payment.amount, currency='KZT', reference=payment.code, manual_review_required=True)

    def verify_event(self, body: bytes, headers: dict) -> VerifiedPayment:
        raise NotImplementedError('Ручная ссылка Kaspi не предоставляет подтверждённых серверных событий.')
