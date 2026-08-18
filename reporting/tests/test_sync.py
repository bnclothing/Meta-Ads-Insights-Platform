import base64
from datetime import date

from django.test import TestCase, override_settings

from reporting.connectors import FetchedPage, HealthStatus, MetaApiError
from reporting.models import ActionMetricDaily, InsightDaily, MetaConnection, RawApiPayload, SyncRun, SyncStatus
from reporting.services.sync import perform_sync


TEST_KEY = base64.urlsafe_b64encode(b"s" * 32).decode("ascii")


def page(endpoint, data):
    return FetchedPage(endpoint=endpoint, request_params={"limit": 500}, payload={"data": data})


class FakeConnector:
    def __init__(self, *, fail_level=None):
        self.fail_level = fail_level

    def test_connection(self):
        return HealthStatus(True, "ok", {"account_id": "456", "name": "ULTEx Test", "currency": "MAD", "timezone_name": "Africa/Casablanca"})

    def health_status(self):
        return self.test_connection()

    def sync_objects(self):
        return {
            "campaigns": [page("campaigns", [{"id": "c1", "name": "Campagne", "objective": "OUTCOME_LEADS", "effective_status": "ACTIVE"}])],
            "adsets": [page("adsets", [{"id": "s1", "campaign_id": "c1", "name": "Ensemble", "effective_status": "ACTIVE"}])],
            "ads": [page("ads", [{"id": "a1", "campaign_id": "c1", "adset_id": "s1", "name": "Publicité", "effective_status": "ACTIVE"}])],
        }

    def sync_insights(self, start, end, levels):
        level = levels[0]
        if level == self.fail_level:
            raise MetaApiError("niveau indisponible", code="2", retryable=False)
        ids = {
            "account": {"account_id": "456", "account_name": "ULTEx Test"},
            "campaign": {"campaign_id": "c1", "campaign_name": "Campagne"},
            "adset": {"campaign_id": "c1", "adset_id": "s1", "adset_name": "Ensemble"},
            "ad": {"campaign_id": "c1", "adset_id": "s1", "ad_id": "a1", "ad_name": "Publicité"},
        }[level]
        row = {
            **ids,
            "account_currency": "MAD",
            "date_start": start.isoformat(),
            "date_stop": end.isoformat(),
            "spend": "100.00",
            "impressions": "1000",
            "reach": "800",
            "clicks": "40",
            "inline_link_clicks": "30",
            "ctr": "4.0",
            "cpc": "2.5",
            "cpm": "100",
            "actions": [{"action_type": "onsite_conversion.lead_grouped", "value": "10"}],
            "cost_per_action_type": [{"action_type": "onsite_conversion.lead_grouped", "value": "10"}],
        }
        return {level: [page(f"insights/{level}", [row])]}


@override_settings(DATA_ENCRYPTION_KEY=TEST_KEY)
class SynchronizationTests(TestCase):
    def setUp(self):
        self.connection = MetaConnection.objects.create(name="Meta", ad_account_external_id="456")

    def _run(self, connector):
        sync = SyncRun.objects.create(
            connection=self.connection,
            requested_start=date(2026, 8, 15),
            requested_end=date(2026, 8, 15),
            levels=["account", "campaign", "adset", "ad"],
        )
        return perform_sync(sync, connector=connector)

    def test_sync_is_idempotent_and_preserves_raw_traceability(self):
        first = self._run(FakeConnector())
        second = self._run(FakeConnector())

        self.assertEqual(first.status, SyncStatus.SUCCESS)
        self.assertEqual(second.status, SyncStatus.SUCCESS)
        self.assertEqual(InsightDaily.objects.count(), 4)
        self.assertEqual(ActionMetricDaily.objects.count(), 4)
        self.assertEqual(RawApiPayload.objects.count(), 14)
        self.assertEqual(set(InsightDaily.objects.values_list("sync_run_id", flat=True)), {second.pk})
        self.assertTrue(all(row.raw_payload_id for row in InsightDaily.objects.all()))

    def test_nonretryable_level_error_marks_import_partial(self):
        sync = self._run(FakeConnector(fail_level="ad"))
        self.assertEqual(sync.status, SyncStatus.PARTIAL)
        self.assertEqual(InsightDaily.objects.count(), 3)
        self.assertEqual(sync.errors.count(), 1)
        self.assertEqual(sync.errors.get().level, "ad")

