import base64
import hashlib
import hmac
import io
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from arena.game_lookup import GoPayLookupProvider, LookupNotFound, LookupUnavailable, MooGoldLookupProvider, _NoRedirect, is_available, lookup, normalize_ids, provider_enabled


CONFIG = {
    "MLBB_LOOKUP_PROVIDER": "moogold",
    "MOOGOLD_PARTNER_ID": "test-partner",
    "MOOGOLD_SECRET_KEY": "test-secret-never-log",
    # Mock-only values, not a real MLBB product or a verified field mapping.
    "MOOGOLD_MLBB_PRODUCT_ID": "999999",
    "MOOGOLD_MLBB_USER_ID_FIELD": "User ID",
    "MOOGOLD_MLBB_SERVER_ID_FIELD": "Server",
    "MOOGOLD_MLBB_CONTRACT_CONFIRMED": True,
    "MLBB_LOOKUP_TIMEOUT": 5,
}


class GameLookupTests(SimpleTestCase):
    def response(self, payload=None, raw=None, status=200):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.read.return_value = raw if raw is not None else json.dumps(payload).encode("utf-8")
        return response

    @override_settings(MLBB_LOOKUP_PROVIDER="disabled")
    @patch("arena.game_lookup._open_request")
    def test_disabled_never_sends_data(self, opened):
        self.assertFalse(provider_enabled())
        self.assertFalse(is_available())
        with self.assertRaises(LookupUnavailable):
            lookup("123", "45")
        opened.assert_not_called()

    @override_settings(**CONFIG)
    @patch("arena.game_lookup._open_request")
    def test_unconfirmed_or_incomplete_configuration_fails_closed(self, opened):
        for changes in [
            {"MOOGOLD_MLBB_CONTRACT_CONFIRMED": False},
            {"MOOGOLD_MLBB_CONTRACT_CONFIRMED": "true"},
            {"MOOGOLD_MLBB_PRODUCT_ID": ""},
            {"MOOGOLD_MLBB_USER_ID_FIELD": ""},
            {"MOOGOLD_MLBB_SERVER_ID_FIELD": ""},
            {"MOOGOLD_MLBB_SERVER_ID_FIELD": "User ID"},
            {"MOOGOLD_MLBB_USER_ID_FIELD": "product-id"},
            {"MOOGOLD_PARTNER_ID": ""},
            {"MOOGOLD_SECRET_KEY": ""},
            {"MLBB_LOOKUP_TIMEOUT": 0},
            {"MLBB_LOOKUP_TIMEOUT": 16},
            {"MLBB_LOOKUP_PROVIDER": "typo-provider"},
        ]:
            with self.subTest(changes=changes), override_settings(**changes):
                self.assertTrue(provider_enabled())
                self.assertFalse(is_available())
                with self.assertRaises(LookupUnavailable):
                    lookup("123", "45")
        opened.assert_not_called()

    def test_ids_are_canonical_ascii_positive_numbers(self):
        self.assertEqual(normalize_ids(" 000123 ", 45), ("123", "45"))
        for value in (None, True, -1, 0, "0", "-1", "1.2", "1e5", "１２３", "123&x=y", "1" * 41, {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                normalize_ids(value, "45")

    @override_settings(**CONFIG)
    @patch("arena.game_lookup.time.time", return_value=1700000000)
    @patch("arena.game_lookup._open_request")
    def test_exact_documented_signature_and_fixed_read_only_endpoint(self, opened, clock):
        opened.return_value = self.response({"status": "true", "username": "Тестовый Ник"})
        self.assertTrue(is_available())
        opened.assert_not_called()
        result = lookup("00123", "0045")
        self.assertEqual((result.nickname, result.provider, result.game_id, result.server_id), ("Тестовый Ник", "moogold", "123", "45"))
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://moogold.com/wp-json/v1/api/product/validate")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"path": "product/validate", "data": {"product-id": "999999", "User ID": "123", "Server": "45"}})
        expected = hmac.new(CONFIG["MOOGOLD_SECRET_KEY"].encode(), request.data + b"1700000000product/validate", hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("Auth"), expected)
        self.assertEqual(request.get_header("Timestamp"), "1700000000")
        self.assertEqual(request.get_header("Authorization"), "Basic " + base64.b64encode(b"test-partner:test-secret-never-log").decode())
        self.assertEqual(opened.call_args.kwargs["timeout"], 5)
        opened.return_value.read.assert_called_once_with(65537)
        opened.assert_called_once()

    @override_settings(**CONFIG)
    @patch("arena.game_lookup._open_request")
    def test_errors_never_reveal_credentials_or_mean_account_not_found(self, opened):
        for failure in (URLError("secret https://host/123?key=test-secret-never-log"), TimeoutError("secret"), HTTPError("https://host", 403, "secret", {}, None)):
            with self.subTest(failure=type(failure).__name__):
                opened.side_effect = failure
                with self.assertRaises(LookupUnavailable) as caught:
                    lookup("123", "45")
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("https", str(caught.exception))

    @override_settings(**CONFIG)
    @patch("arena.game_lookup._open_request")
    def test_unrecognized_response_is_unavailable_not_invalid_player(self, opened):
        for payload in ({"err_code": 403, "err_message": "User not found or not authorized"}, {"status": "false"}, {"status": 1, "username": "Name"}, {"status": "true", "username": "Name", "err_code": 119}, [], {}, {"status": "true", "username": ""}, {"status": "true", "username": None}, {"status": "true", "username": "N" * 101}, {"status": "true", "username": "Line\nBreak"}):
            with self.subTest(payload=payload):
                opened.return_value = self.response(payload)
                with self.assertRaises(LookupUnavailable):
                    lookup("123", "45")
        for raw in (b"not-json", b"\xff", b"x" * 65537):
            opened.return_value = self.response(raw=raw)
            with self.assertRaises(LookupUnavailable):
                lookup("123", "45")

    def test_redirects_are_not_followed_and_credentials_are_not_in_repr(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://untrusted.invalid/"))
        provider = MooGoldLookupProvider("test-partner", "test-secret", "999999", "User ID", "Server")
        self.assertNotIn("test-secret", repr(provider))
        self.assertNotIn("test-partner", repr(provider))


@override_settings(MLBB_LOOKUP_PROVIDER="gopay", MLBB_LOOKUP_TIMEOUT=5)
class GoPayLookupTests(SimpleTestCase):
    response = GameLookupTests.response

    @patch("arena.game_lookup._open_request")
    def test_fixed_validation_post_without_auth_order_or_query(self, opened):
        self.assertTrue(provider_enabled())
        self.assertTrue(is_available())
        opened.assert_not_called()
        for status in (200, 201):
            with self.subTest(status=status):
                opened.reset_mock()
                opened.return_value = self.response({"message": "Success", "data": {"username": "Test+Name", "countryOrigin": "id"}}, status=status)
                result = lookup("00123", "0045")
                self.assertEqual((result.nickname, result.provider, result.game_id, result.server_id), ("Test Name", "gopay", "123", "45"))
                request = opened.call_args.args[0]
                self.assertEqual(request.full_url, "https://gopay.co.id/games/v1/order/user-account")
                self.assertEqual(request.method, "POST")
                self.assertEqual(json.loads(request.data), {"code": "MOBILE_LEGENDS", "data": {"userId": "123", "zoneId": "45"}})
                self.assertIsNone(request.get_header("Authorization"))
                self.assertIsNone(request.get_header("Cookie"))
                self.assertEqual(opened.call_args.kwargs["timeout"], 5)
                opened.return_value.read.assert_called_once_with(65537)
                opened.assert_called_once()

    @patch("arena.game_lookup._open_request")
    def test_nickname_decodes_once_and_rejects_invalid_decoded_values(self, opened):
        for encoded, expected in (("A%2BB+C", "A+B C"), ("%252B", "%2B"), ("%D0%9D%D0%B8%D0%BA", "Ник")):
            opened.return_value = self.response({"message": "Success", "data": {"username": encoded}})
            self.assertEqual(lookup("123", "45").nickname, expected)
        for name in (None, 123, "", "+", "%0A", "%FF", "A" * 101, "\ud800"):
            with self.subTest(name=repr(name)):
                opened.return_value = self.response({"message": "Success", "data": {"username": name}})
                with self.assertRaises(LookupUnavailable):
                    lookup("123", "45")

    @patch("arena.game_lookup._open_request")
    def test_only_exact_not_found_error_means_unknown_pair(self, opened):
        opened.side_effect = HTTPError(GoPayLookupProvider.URL, 404, "Not Found", {}, io.BytesIO(json.dumps(GoPayLookupProvider.NOT_FOUND).encode()))
        with self.assertRaises(LookupNotFound):
            lookup("123", "45")
        cases = [(404, {}), (404, {"message": "Not found"}), (403, GoPayLookupProvider.NOT_FOUND), (500, GoPayLookupProvider.NOT_FOUND), (302, {}), (404, {**GoPayLookupProvider.NOT_FOUND, "extra": "changed contract"})]
        for status, payload in cases:
            with self.subTest(status=status, payload=payload):
                opened.side_effect = HTTPError(GoPayLookupProvider.URL, status, "error", {}, io.BytesIO(json.dumps(payload).encode()))
                with self.assertRaises(LookupUnavailable):
                    lookup("123", "45")

    @patch("arena.game_lookup._open_request")
    def test_malformed_or_ambiguous_success_is_unavailable(self, opened):
        for payload in ([], {}, {"message": "Success"}, {"message": "Success", "data": []}, {"message": "Failure", "data": {"username": "Name"}}, {"message": "Success", "data": {"username": "Name"}, "error": "Failure"}):
            with self.subTest(payload=payload):
                opened.return_value = self.response(payload)
                with self.assertRaises(LookupUnavailable):
                    lookup("123", "45")
        for raw in (b"not-json", b"\xff", b"x" * 65537):
            opened.return_value = self.response(raw=raw)
            with self.assertRaises(LookupUnavailable):
                lookup("123", "45")
        opened.return_value = self.response({"message": "Success", "data": {"username": "Name"}}, status=202)
        with self.assertRaises(LookupUnavailable):
            lookup("123", "45")

    @patch("arena.game_lookup._open_request")
    def test_transport_errors_are_private_and_no_retries(self, opened):
        for failure in (TimeoutError("private 123/45"), URLError("private upstream response"), OSError("private network error")):
            opened.reset_mock()
            opened.side_effect = failure
            with self.assertRaises(LookupUnavailable) as caught:
                lookup("123", "45")
            self.assertNotIn("private", str(caught.exception))
            opened.assert_called_once()

    @patch("arena.game_lookup._open_request")
    def test_invalid_configuration_and_ids_never_send(self, opened):
        for timeout in (0, 16, True, "5", float("nan"), float("inf")):
            with self.subTest(timeout=timeout), override_settings(MLBB_LOOKUP_TIMEOUT=timeout):
                self.assertTrue(provider_enabled())
                self.assertFalse(is_available())
                with self.assertRaises(LookupUnavailable):
                    lookup("123", "45")
        with self.assertRaises(ValidationError):
            lookup("123&inject=yes", "45")
        opened.assert_not_called()

    @patch("arena.game_lookup.build_opener")
    def test_requests_use_redirect_blocking_opener(self, builder):
        builder.return_value.open.return_value = self.response({"message": "Success", "data": {"username": "Name"}})
        lookup("123", "45")
        self.assertIsInstance(builder.call_args.args[0], _NoRedirect)
        builder.return_value.open.assert_called_once()
