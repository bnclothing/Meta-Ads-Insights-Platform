import base64
import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reporting.connectors.base import HealthStatus
from reporting.models import AdAccount, ConnectionStatus, InsightDaily, InsightLevel, MetaConnection, SyncRun, SyncStatus


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

    def test_connected_connection_without_account_starts_initial_sync_automatically(self):
        MetaConnection.objects.create(
            name="Meta",
            ad_account_external_id="456",
            status=ConnectionStatus.CONNECTED,
            is_active=True,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"), {"date": "2026-08-16"})

        self.assertContains(response, "Préparation de votre vue")
        self.assertContains(response, "sync-loading-card")
        self.assertContains(response, "data-auto-sync")
        self.assertContains(response, "data-sync-now")
        self.assertContains(response, "Connexion au compte et récupération des données")
        self.assertContains(response, 'data-start-date="2026-05-19"')
        self.assertNotContains(response, "Configurer Meta")

    def test_successful_connection_test_queues_initial_90_day_backfill(self):
        connection = MetaConnection.objects.create(
            name="Nouveau compte",
            ad_account_external_id="456",
            is_active=True,
        )
        self.client.force_login(self.user)

        with (
            patch(
                "reporting.views.MetaMarketingConnector.health_status",
                return_value=HealthStatus(True, "Connexion Meta opérationnelle.", {"account_id": "456"}),
            ),
            patch("reporting.views.synchronize_meta.delay") as dispatch,
        ):
            dispatch.return_value.id = "initial-backfill-task"
            response = self.client.post(
                reverse("api_meta_test"),
                data=json.dumps({"connection_id": connection.pk}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        run = SyncRun.objects.get(connection=connection)
        self.assertEqual(run.trigger, "initial_backfill")
        self.assertEqual(run.requested_end - run.requested_start, timedelta(days=89))
        self.assertEqual(set(run.levels), set(InsightLevel.values))
        self.assertTrue(response.json()["sync"]["created"])
        self.assertIn("Import automatique des 90 derniers jours démarré", response.json()["message"])

    def test_retesting_connection_does_not_repeat_completed_initial_backfill(self):
        connection = MetaConnection.objects.create(
            name="Compte déjà importé",
            ad_account_external_id="456",
            is_active=True,
        )
        SyncRun.objects.create(
            connection=connection,
            requested_start=date(2026, 1, 1),
            requested_end=date(2026, 3, 31),
            levels=list(InsightLevel.values),
            status=SyncStatus.SUCCESS,
            trigger="initial_backfill",
        )
        self.client.force_login(self.user)

        with (
            patch(
                "reporting.views.MetaMarketingConnector.health_status",
                return_value=HealthStatus(True, "Connexion Meta opérationnelle.", {}),
            ),
            patch("reporting.views.synchronize_meta.delay") as dispatch,
        ):
            response = self.client.post(
                reverse("api_meta_test"),
                data=json.dumps({"connection_id": connection.pk}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("sync", response.json())
        dispatch.assert_not_called()
        self.assertEqual(SyncRun.objects.count(), 1)

    def test_uncovered_historical_range_is_loaded_automatically(self):
        connection = MetaConnection.objects.create(
            name="Meta",
            ad_account_external_id="456",
            status=ConnectionStatus.CONNECTED,
            is_active=True,
        )
        AdAccount.objects.create(connection=connection, external_id="456", name="Compte actuel")
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"), {"start": "2026-04-01", "end": "2026-04-30"})

        self.assertTrue(response.context["automatic_sync_enabled"])
        self.assertEqual(response.context["automatic_sync_start"], date(2026, 4, 1))
        self.assertContains(response, "data-auto-sync")
        self.assertContains(response, 'data-start-date="2026-04-01"')
        self.assertContains(response, 'data-end-date="2026-04-30"')

    def test_completed_empty_range_is_remembered_without_auto_sync_loop(self):
        connection = MetaConnection.objects.create(
            name="Meta",
            ad_account_external_id="456",
            status=ConnectionStatus.CONNECTED,
            is_active=True,
        )
        account = AdAccount.objects.create(connection=connection, external_id="456", name="Compte actuel")
        SyncRun.objects.create(
            connection=connection,
            account=account,
            requested_start=date(2026, 4, 1),
            requested_end=date(2026, 4, 30),
            levels=list(InsightLevel.values),
            status=SyncStatus.SUCCESS,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"), {"start": "2026-04-01", "end": "2026-04-30"})

        self.assertFalse(response.context["automatic_sync_enabled"])
        self.assertNotContains(response, "<div hidden data-auto-sync")
        self.assertContains(response, "Meta n’a retourné aucune activité")

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

    def test_dashboard_uses_campaign_primary_results_when_account_rows_are_ambiguous(self):
        connection = MetaConnection.objects.create(
            name="Meta",
            ad_account_external_id="456",
            status=ConnectionStatus.CONNECTED,
            is_active=True,
        )
        account = AdAccount.objects.create(connection=connection, external_id="456", name="Compte actuel", currency="USD")

        def add_row(day, level, object_id, name, spend, results, action_type=""):
            spend_value = Decimal(spend)
            result_value = Decimal(results) if results is not None else None
            return InsightDaily.objects.create(
                account=account,
                level=level,
                object_external_id=object_id,
                object_name=name,
                date=day,
                currency="USD",
                spend=spend_value,
                impressions=100,
                results=result_value,
                cost_per_result=(spend_value / result_value) if result_value else None,
                result_action_type=action_type,
                result_label="Meta résultat",
            )

        account_days = [
            (date(2026, 4, 1), "17.47", "1"),
            (date(2026, 4, 2), "13.29", "10"),
            (date(2026, 4, 3), "12.54", "2"),
            (date(2026, 4, 4), "16.69", None),
        ]
        for day, spend, results in account_days:
            add_row(day, InsightLevel.ACCOUNT, "456", "Compte actuel", spend, results, "lead" if results is not None else "")

        campaign_days = [
            (date(2026, 4, 1), "17.47", "1"),
            (date(2026, 4, 2), "13.29", "0"),
            (date(2026, 4, 3), "12.13", "0"),
            (date(2026, 4, 4), "16.69", "0"),
        ]
        for day, spend, results in campaign_days:
            add_row(day, InsightLevel.CAMPAIGN, "campaign-vsl", "Campagne VSL Vidéo 24 Mars", spend, results, "meta_objective:website_lead")
        add_row(date(2026, 4, 3), InsightLevel.CAMPAIGN, "campaign-other", "Campagne le 30 Mars VSL", "0.41", None)
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"), {"start": "2026-04-01", "end": "2026-04-30"})

        self.assertEqual(response.context["kpis"][0]["value"], "59.99")
        self.assertEqual(response.context["kpis"][1]["value"], "1.00")
        self.assertEqual(response.context["kpis"][2]["value"], "59.99")
        self.assertEqual([point["results"] for point in response.context["trend"]], [1.0, 0.0, 0.0, 0.0])
        self.assertEqual(response.context["campaigns"][0]["results"], "1.00")

    def test_selected_connection_persists_between_pages(self):
        first_connection = MetaConnection.objects.create(name="Premier", ad_account_external_id="111", status=ConnectionStatus.CONNECTED)
        second_connection = MetaConnection.objects.create(name="Deuxième", ad_account_external_id="222", status=ConnectionStatus.CONNECTED)
        AdAccount.objects.create(connection=first_connection, external_id="111", name="Premier compte")
        second_account = AdAccount.objects.create(connection=second_connection, external_id="222", name="Deuxième compte")
        self.client.force_login(self.user)

        dashboard_response = self.client.get(reverse("dashboard"), {"connection": second_connection.pk})
        performance_response = self.client.get(reverse("performance"))

        self.assertEqual(dashboard_response.context["selected_connection"].pk, second_connection.pk)
        self.assertEqual(dashboard_response.context["account"]["id"], second_account.pk)
        self.assertEqual(performance_response.context["account"].pk, second_account.pk)
        self.assertContains(performance_response, "Deuxième")

    def test_manual_sync_targets_requested_connection(self):
        first_connection = MetaConnection.objects.create(name="Premier", ad_account_external_id="111", status=ConnectionStatus.CONNECTED)
        second_connection = MetaConnection.objects.create(name="Deuxième", ad_account_external_id="222", status=ConnectionStatus.CONNECTED)
        self.client.force_login(self.user)

        with patch("reporting.views.synchronize_meta.delay") as dispatch:
            dispatch.return_value.id = "task-second-account"
            response = self.client.post(
                reverse("api_syncs"),
                data=json.dumps(
                    {
                        "connection_id": second_connection.pk,
                        "start": "2026-08-15",
                        "end": "2026-08-16",
                        "levels": ["account"],
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        sync_run = SyncRun.objects.get()
        self.assertEqual(sync_run.connection, second_connection)
        self.assertNotEqual(sync_run.connection, first_connection)

    def test_automatic_sync_reuses_covering_job_already_in_progress(self):
        connection = MetaConnection.objects.create(name="Meta", ad_account_external_id="222", status=ConnectionStatus.CONNECTED)
        existing = SyncRun.objects.create(
            connection=connection,
            requested_start=date(2026, 3, 1),
            requested_end=date(2026, 4, 30),
            levels=list(InsightLevel.values),
            status=SyncStatus.RUNNING,
            task_id="existing-task",
        )
        self.client.force_login(self.user)

        with patch("reporting.views.synchronize_meta.delay") as dispatch:
            response = self.client.post(
                reverse("api_syncs"),
                data=json.dumps(
                    {
                        "connection_id": connection.pk,
                        "start": "2026-04-01",
                        "end": "2026-04-30",
                        "levels": list(InsightLevel.values),
                        "automatic": True,
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["id"], existing.pk)
        self.assertFalse(response.json()["created"])
        dispatch.assert_not_called()
        self.assertEqual(SyncRun.objects.count(), 1)

    def test_settings_can_add_second_connection_without_overwriting_first(self):
        first_connection = MetaConnection.objects.create(name="Premier", ad_account_external_id="111", status=ConnectionStatus.CONNECTED)
        self.client.force_login(self.user)

        response = self.client.post(
            f"{reverse('settings')}?new=1",
            data={
                "action": "save_connection",
                "connection-name": "Deuxième",
                "connection-app_id": "222222",
                "connection-ad_account_external_id": "act_222",
                "connection-api_version": "v25.0",
                "connection-is_active": "on",
                "connection-access_token": "second-account-system-token",
                "connection-app_secret": "second-account-app-secret",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(MetaConnection.objects.count(), 2)
        first_connection.refresh_from_db()
        self.assertEqual(first_connection.ad_account_external_id, "111")
        second_connection = MetaConnection.objects.exclude(pk=first_connection.pk).get()
        self.assertEqual(second_connection.ad_account_external_id, "222")
        self.assertEqual(self.client.session["selected_meta_connection_id"], second_connection.pk)

    def test_duplicate_ad_account_connection_is_rejected(self):
        MetaConnection.objects.create(name="Premier", ad_account_external_id="111")
        self.client.force_login(self.user)

        response = self.client.post(
            f"{reverse('settings')}?new=1",
            data={
                "action": "save_connection",
                "connection-name": "Doublon",
                "connection-app_id": "333333",
                "connection-ad_account_external_id": "act_111",
                "connection-api_version": "v25.0",
                "connection-is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(MetaConnection.objects.count(), 1)
        self.assertContains(response, "déjà configuré")
