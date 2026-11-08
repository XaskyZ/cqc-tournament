"""Read-only MLBB nickname lookup with explicit, verified provider configuration.

No provider is enabled by default. A nickname lookup never proves ownership.
MooGold's public specification is generic: operators must establish the exact
MLBB product/field mapping and full ID+zone semantics before enabling it.
"""

import base64
import hashlib
import hmac
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import unquote_plus
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.exceptions import ValidationError


class LookupUnavailable(Exception):
    """No reliable lookup could be completed; never means an invalid account."""


class LookupNotFound(Exception):
    """The provider returned its recognized account-not-found result."""


@dataclass(frozen=True)
class LookupResult:
    nickname: str
    provider: str
    game_id: str
    server_id: str


class LookupProvider(Protocol):
    def lookup(self, game_id: str, server_id: str) -> LookupResult: ...


def normalize_ids(game_id, server_id):
    normalized = []
    for value, label in ((game_id, "Game ID"), (server_id, "Server ID")):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValidationError(f"{label}: укажите положительный числовой ID.")
        value = str(value).strip()
        if not re.fullmatch(r"[0-9]{1,40}", value) or int(value) == 0:
            raise ValidationError(f"{label}: укажите от 1 до 40 цифр; ID должен быть положительным.")
        normalized.append(str(int(value)))
    return tuple(normalized)


class DisabledLookupProvider:
    def lookup(self, game_id, server_id):
        normalize_ids(game_id, server_id)
        raise LookupUnavailable("Автоматическая проверка игрового ника пока не подключена. Обратитесь к организатору.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward partner credentials to a redirect target.
        return None


def _open_request(request, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


@dataclass(frozen=True)
class GoPayLookupProvider:
    """Public account validation used by GoPay Games, without creating an order.

    The request and both success/not-found responses were checked against a
    public repository's example pair. Regional coverage is not guaranteed.
    """

    timeout: float = 5.0
    URL = "https://gopay.co.id/games/v1/order/user-account"
    MAX_RESPONSE_BYTES = 65536
    NOT_FOUND = {
        "statusCode": 404,
        "message": "Invalid User Account: The user account is not recognized. Please confirm your account information.",
        "error": "Not Found",
    }

    def _validate_configuration(self):
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not 1 <= self.timeout <= 15:
            raise LookupUnavailable("Некорректный таймаут проверки игрового ника.")

    def _read_json(self, response):
        raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise LookupUnavailable("Сервис проверки вернул некорректный ответ.")
        return json.loads(raw.decode("utf-8"))

    def lookup(self, game_id, server_id):
        game_id, server_id = normalize_ids(game_id, server_id)
        self._validate_configuration()
        body = json.dumps({"code": "MOBILE_LEGENDS", "data": {
            "userId": game_id, "zoneId": server_id,
        }}, separators=(",", ":")).encode("utf-8")
        request = Request(self.URL, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "CQC-Tournament/1.0",
        })
        try:
            try:
                with _open_request(request, timeout=self.timeout) as response:
                    if response.status not in (200, 201):
                        raise LookupUnavailable("Сервис проверки игрового ника временно недоступен.")
                    result = self._read_json(response)
            except HTTPError as error:
                try:
                    if error.code == 404 and self._read_json(error) == self.NOT_FOUND:
                        raise LookupNotFound("Аккаунт с указанными Game ID и Server ID не найден.") from None
                finally:
                    error.close()
                raise LookupUnavailable("Сервис проверки игрового ника временно недоступен.") from None
        except (URLError, OSError, TimeoutError, ValueError, UnicodeError):
            raise LookupUnavailable("Сервис проверки игрового ника временно недоступен. Повторите позже.") from None
        if (not isinstance(result, dict) or result.get("message") != "Success"
                or not isinstance(result.get("data"), dict)
                or "error" in result or "statusCode" in result):
            raise LookupUnavailable("Сервис не подтвердил игровой аккаунт. Повторите проверку позже.")
        nickname = result["data"].get("username")
        if not isinstance(nickname, str):
            raise LookupUnavailable("Сервис проверки не вернул корректный игровой ник.")
        try:
            # The provider encodes spaces as '+'. Decode once so literal %2B
            # remains '+', and double-encoded strings are not decoded twice.
            nickname = unquote_plus(nickname, encoding="utf-8", errors="strict")
        except UnicodeError:
            raise LookupUnavailable("Сервис проверки не вернул корректный игровой ник.") from None
        if (not nickname.strip() or len(nickname) > 100
                or any(unicodedata.category(ch) in {"Cc", "Cs"} for ch in nickname)):
            raise LookupUnavailable("Сервис проверки не вернул корректный игровой ник.")
        return LookupResult(nickname=nickname, provider="gopay", game_id=game_id, server_id=server_id)


@dataclass(frozen=True)
class MooGoldLookupProvider:
    partner_id: str = field(repr=False)
    secret_key: str = field(repr=False)
    product_id: str
    user_id_field: str
    server_id_field: str
    contract_confirmed: bool = False
    timeout: float = 5.0

    PATH = "product/validate"
    URL = "https://moogold.com/wp-json/v1/api/product/validate"
    MAX_RESPONSE_BYTES = 65536

    def _validate_configuration(self):
        # No guessed MLBB product ID or guessed Server/Zone field is provided.
        if self.contract_confirmed is not True:
            raise LookupUnavailable("Контракт проверки MLBB ещё не подтверждён организатором.")
        if not isinstance(self.partner_id, str) or not self.partner_id.strip() or ":" in self.partner_id:
            raise LookupUnavailable("Проверка игрового ника не настроена.")
        if not isinstance(self.secret_key, str) or not self.secret_key:
            raise LookupUnavailable("Проверка игрового ника не настроена.")
        if not re.fullmatch(r"[1-9][0-9]{0,39}", str(self.product_id)):
            raise LookupUnavailable("Не настроен подтверждённый продукт MLBB.")
        names = (self.user_id_field, self.server_id_field)
        if (any(not isinstance(name, str) or not 1 <= len(name) <= 80 or name != name.strip()
                or name in {"product-id", "path"} or any(unicodedata.category(ch).startswith("C") for ch in name)
                for name in names) or names[0] == names[1]):
            raise LookupUnavailable("Не настроены подтверждённые поля Game ID и Server ID.")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not 1 <= self.timeout <= 15:
            raise LookupUnavailable("Некорректный таймаут проверки игрового ника.")

    def lookup(self, game_id, server_id):
        game_id, server_id = normalize_ids(game_id, server_id)
        self._validate_configuration()
        body = json.dumps({"path": self.PATH, "data": {
            "product-id": str(self.product_id),
            self.user_id_field: game_id,
            self.server_id_field: server_id,
        }}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = hmac.new(self.secret_key.encode("utf-8"), body + timestamp.encode("ascii") + self.PATH.encode("ascii"), hashlib.sha256).hexdigest()
        authorization = base64.b64encode(f"{self.partner_id}:{self.secret_key}".encode("utf-8")).decode("ascii")
        request = Request(self.URL, data=body, method="POST", headers={
            "Authorization": f"Basic {authorization}", "auth": signature,
            "timestamp": timestamp, "Content-Type": "application/json", "Accept": "application/json",
        })
        try:
            with _open_request(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise LookupUnavailable("Сервис проверки игрового ника временно недоступен.")
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
                if len(raw) > self.MAX_RESPONSE_BYTES:
                    raise LookupUnavailable("Сервис проверки вернул некорректный ответ.")
                result = json.loads(raw.decode("utf-8"))
        except (HTTPError, URLError, OSError, TimeoutError, ValueError, UnicodeError):
            # Do not expose URLs, credentials, raw responses or player data.
            raise LookupUnavailable("Сервис проверки игрового ника временно недоступен. Повторите позже.") from None
        if (not isinstance(result, dict) or not (result.get("status") is True or result.get("status") == "true")
                or "err_code" in result or "err_message" in result):
            # MooGold's documented errors do not distinguish player-not-found
            # from access/authorization errors. Do not invent that distinction.
            raise LookupUnavailable("Сервис не подтвердил игровой аккаунт. Нужна повторная или ручная проверка.")
        nickname = result.get("username")
        if (not isinstance(nickname, str) or not nickname.strip() or len(nickname) > 100
                or any(unicodedata.category(ch) in {"Cc", "Cs"} for ch in nickname)):
            raise LookupUnavailable("Сервис проверки не вернул корректный игровой ник.")
        return LookupResult(nickname=nickname, provider="moogold", game_id=game_id, server_id=server_id)


def _moogold_provider():
    return MooGoldLookupProvider(
        partner_id=getattr(settings, "MOOGOLD_PARTNER_ID", ""),
        secret_key=getattr(settings, "MOOGOLD_SECRET_KEY", ""),
        product_id=getattr(settings, "MOOGOLD_MLBB_PRODUCT_ID", ""),
        user_id_field=getattr(settings, "MOOGOLD_MLBB_USER_ID_FIELD", ""),
        server_id_field=getattr(settings, "MOOGOLD_MLBB_SERVER_ID_FIELD", ""),
        contract_confirmed=getattr(settings, "MOOGOLD_MLBB_CONTRACT_CONFIRMED", False),
        timeout=getattr(settings, "MLBB_LOOKUP_TIMEOUT", 5),
    )


def provider_enabled():
    """Selected integrations fail closed even if their configuration is broken."""
    return getattr(settings, "MLBB_LOOKUP_PROVIDER", "disabled") != "disabled"


def is_available():
    """Configuration readiness only; makes no request or service-health claim."""
    provider_name = getattr(settings, "MLBB_LOOKUP_PROVIDER", "disabled")
    if provider_name == "gopay":
        provider = GoPayLookupProvider(timeout=getattr(settings, "MLBB_LOOKUP_TIMEOUT", 5))
    elif provider_name == "moogold":
        provider = _moogold_provider()
    else:
        return False
    try:
        provider._validate_configuration()
    except LookupUnavailable:
        return False
    return True


def lookup(game_id, server_id):
    """Resolve a nickname; caller owns consent, authorization and persistence."""
    game_id, server_id = normalize_ids(game_id, server_id)
    provider = getattr(settings, "MLBB_LOOKUP_PROVIDER", "disabled")
    if provider == "disabled":
        return DisabledLookupProvider().lookup(game_id, server_id)
    if provider == "gopay":
        return GoPayLookupProvider(timeout=getattr(settings, "MLBB_LOOKUP_TIMEOUT", 5)).lookup(game_id, server_id)
    if provider != "moogold":
        raise LookupUnavailable("Выбранный сервис проверки игрового ника не поддерживается.")
    return _moogold_provider().lookup(game_id, server_id)
