import base64
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reporting.models import AdAccount, ConnectionStatus, InsightDaily, InsightLevel, MetaConnection


@override_settings(DATA_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"v" * 32).decode("ascii"))
class AuthenticationAndApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator", password="A-strong-local-password-2026")

    def test_private_pages_and_api_require_authentication(self):
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)
        self.assertEqual(self.client.get(reverse("api_dashboard")).status_code, 302)
        self.assertEqual(self.client.get(reverse("health")).status_code, 200)

    def test_authenticated_post_without_csrf_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("api_syncs"), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_settings_api_masks_token_and_never_returns_plaintext(self):
        connection = MetaConnection(name="Meta", ad_account_external_id="456")
        connection.set_access_token("very-secret-meta-token")
        connection.save()
        AdAccount.objects.create(connection=connection, external_id="456", name="ULTEx")
        self.client.force_login(self.user)
        response = self.client.get(reverse("api_settings"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn("very-secret-meta-token", body)
        self.assertIn("oken", body)

    def test_reports_api_rejects_invalid_date_range(self):
        connection = MetaConnection.objects.create(name="Meta", ad_account_external_id="456")
        AdAccount.objects.create(connection=connection, external_id="456", name="ULTEx")
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("api_reports"),
            data='{"start":"2026-08-10","end":"2026-08-01"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_connected_connection_without_account_shows_initial_sync(self):
        MetaConnection.objects.create(
            name="Meta",
            ad_account_external_id="456",
            status=ConnectionStatus.CONNECTED,
            is_active=True,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"), {"date": "2026-08-16"})

        self.assertContains(response, "Importez votre première journée")
        self.assertContains(response, "data-sync-now")
        self.assertContains(response, "Importer les 90 derniers jours")
        self.assertContains(response, 'data-start-date="2026-05-19"')
        self.assertNotContains(response, "Configurer Meta")

    def test_dashboard_uses_currently_configured_ad_account(self):
        connection = MetaConnection.objects.create(
            name="Meta",
            ad_account_external_id="new-account",
            status=ConnectionStatus.CONNECTED,
            is_active=True,
        )
        AdAccount.objects.create(connection=connection, external_id="old-account", name="Ancien compte")
        current = AdAccount.objects.create(connection=connection, external_id="new-account", name="Compte actuel")
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"), {"date": "2026-08-16"})

        self.assertEqual(response.context["account"]["id"], current.pk)

    def test_dashboard_aggregates_selected_range_and_compares_previous_range(self):
        connection = MetaConnection.objects.create(
            name="Meta",
            ad_account_external_id="456",
            status=ConnectionStatus.CONNECTED,
            is_active=True,
        )
        account = AdAccount.objects.create(connection=connection, external_id="456", name="Compte actuel", currency="USD")

        def insight(day, spend, results, impressions, *, level=InsightLevel.ACCOUNT, object_id="456"):
            return InsightDaily.objects.create(
                account=account,
                level=level,
                object_external_id=object_id,
                object_name="Compte actuel" if level == InsightLevel.ACCOUNT else "Campagne test",
                date=day,
                currency="USD",
                spend=Decimal(spend),
                impressions=impressions,
                results=Decimal(results),
                cost_per_result=Decimal(spend) / Decimal(results),
                result_label="Meta résultat",
            )

        insight(date(2026, 8, 13), "50", "5", 500)
        insight(date(2026, 8, 14), "100", "10", 1000)
        insight(date(2026, 8, 15), "100", "10", 1000)
        insight(date(2026, 8, 16), "200", "20", 2000)
        insight(date(2026, 8, 15), "100", "10", 1000, level=InsightLevel.CAMPAIGN, object_id="campaign-1")
        insight(date(2026, 8, 16), "200", "20", 2000, level=InsightLevel.CAMPAIGN, object_id="campaign-1")
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"), {"start": "2026-08-15", "end": "2026-08-16"})

        self.assertEqual(response.context["selected_start"], date(2026, 8, 15))
        self.assertEqual(response.context["selected_end"], date(2026, 8, 16))
        self.assertEqual(response.context["kpis"][0]["value"], "300.00")
        self.assertEqual(response.context["kpis"][0]["delta"], "100.0")
        self.assertEqual(response.context["campaigns"][0]["spend"], "300.00")
        self.assertEqual(response.context["campaigns"][0]["results"], "30.00")
        self.assertContains(response, 'name="start" value="2026-08-15"')
        self.assertContains(response, 'name="end" value="2026-08-16"')

        api_response = self.client.get(reverse("api_dashboard"), {"start": "2026-08-15", "end": "2026-08-16"})
        self.assertEqual(api_response.json()["start"], "2026-08-15")
        self.assertEqual(api_response.json()["end"], "2026-08-16")
