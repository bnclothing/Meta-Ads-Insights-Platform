import base64
from datetime import date

from django.test import TestCase, override_settings

from reporting.connectors import MetaMarketingConnector
from reporting.models import MetaConnection


TEST_KEY = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")


@override_settings(DATA_ENCRYPTION_KEY=TEST_KEY)
class SecretAndConnectorTests(TestCase):
    def _connection(self):
        connection = MetaConnection(
            name="Meta test",
            app_id="123",
            ad_account_external_id="456",
            api_version="v25.0",
        )
        connection.set_access_token("token-ultex-super-secret")
        connection.set_app_secret("app-secret-ultex")
        connection.save()
        return connection

    def test_secrets_are_encrypted_and_round_trip(self):
        connection = self._connection()
        connection.refresh_from_db()
        self.assertNotIn("token-ultex-super-secret", connection.access_token_encrypted)
        self.assertNotIn("app-secret-ultex", connection.app_secret_encrypted)
        self.assertEqual(connection.get_access_token(), "token-ultex-super-secret")
        self.assertEqual(connection.get_app_secret(), "app-secret-ultex")
        self.assertEqual(connection.masked_token, "••••••••cret")

    def test_pagination_and_raw_pages_redact_authentication(self):
        connection = self._connection()
        calls = []

        def transport(method, url, params):
            calls.append((method, url, params))
            if "after=cursor" in url:
                return {"data": [{"id": "2"}]}
            return {
                "data": [{"id": "1"}],
                "paging": {
                    "next": "https://graph.facebook.com/v25.0/act_456/insights?after=cursor&access_token=token-ultex-super-secret&appsecret_proof=proof"
                },
            }

        connector = MetaMarketingConnector(connection, transport=transport)
        pages = connector._paginate("act_456/insights", {"limit": 1})

        self.assertEqual(len(pages), 2)
        self.assertIn("access_token", calls[0][2])
        self.assertIn("appsecret_proof", calls[0][2])
        self.assertNotIn("access_token", pages[0].request_params)
        self.assertNotIn("appsecret_proof", pages[0].request_params)
        serialized = repr(pages)
        self.assertNotIn("token-ultex-super-secret", serialized)
        self.assertNotIn("appsecret_proof=proof", serialized)

    def test_insight_request_is_daily_and_read_only(self):
        connection = self._connection()
        connector = MetaMarketingConnector(connection, transport=lambda *_: {"data": []})
        params = connector._insight_params(date(2026, 8, 1), date(2026, 8, 2), "campaign")
        self.assertEqual(params["time_increment"], 1)
        self.assertEqual(params["level"], "campaign")
        self.assertEqual(params["use_unified_attribution_setting"], "true")

