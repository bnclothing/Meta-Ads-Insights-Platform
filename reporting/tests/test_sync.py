import base64
from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from reporting.connectors import FetchedPage, HealthStatus, MetaApiError
from reporting.models import ActionMetricDaily, InsightDaily, MetaConnection, RawApiPayload, SyncRun, SyncStatus
from reporting.services.sync import perform_sync
from reporting.tasks import daily_meta_cycle
from reporting.views import _dashboard_payload


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


class ConversionLeadConnector(FakeConnector):
    def sync_insights(self, start, end, levels):
        result = super().sync_insights(start, end, levels)
        row = result[levels[0]][0].payload["data"][0]
        row["spend"] = "59.58"
        row["actions"] = [{"action_type": "link_click", "value": "10"}]
        row["cost_per_action_type"] = [{"action_type": "link_click", "value": "5.958"}]
        row["conversion_leads"] = [{"action_type": "website", "value": "1"}]
        row["cost_per_conversion_lead"] = [{"action_type": "website", "value": "59.58"}]
        return result


class NewLeadActionConnector(FakeConnector):
    def sync_insights(self, start, end, levels):
        result = super().sync_insights(start, end, levels)
        row = result[levels[0]][0].payload["data"][0]
        row["actions"] = [{"action_type": "onsite_web_lead", "value": "1"}]
        row["cost_per_action_type"] = [{"action_type": "onsite_web_lead", "value": "100"}]
        return result


class MetaObjectiveResultConnector(FakeConnector):
    def sync_insights(self, start, end, levels):
        result = super().sync_insights(start, end, levels)
        first = result[levels[0]][0].payload["data"][0]
        first["spend"] = "59.58"
        first["actions"] = [
            {"action_type": "lead", "value": "1"},
            {"action_type": "offsite_conversion.fb_pixel_lead", "value": "1"},
        ]
        first["cost_per_result"] = [
            {"indicator": "actions:offsite_conversion.fb_pixel_lead", "values": [{"value": "59.58"}]}
        ]
        second = deepcopy(first)
        second["date_start"] = "2026-08-16"
        second["date_stop"] = "2026-08-16"
        second["spend"] = "13.29"
        second["actions"] = [{"action_type": "lead", "value": "10"}]
        second.pop("cost_per_result")
        result[levels[0]][0].payload["data"].append(second)
        return result


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

    def test_dedicated_conversion_lead_field_is_not_discarded(self):
        self._run(ConversionLeadConnector())

        rows = InsightDaily.objects.all()
        self.assertEqual(set(rows.values_list("results", flat=True)), {1})
        self.assertEqual(set(rows.values_list("cost_per_result", flat=True)), {Decimal("59.58")})
        self.assertEqual(set(rows.values_list("result_action_type", flat=True)), {"conversion_leads:website"})

    def test_single_new_lead_action_is_selected_without_summing(self):
        self._run(NewLeadActionConnector())

        rows = InsightDaily.objects.all()
        self.assertEqual(set(rows.values_list("results", flat=True)), {1})
        self.assertEqual(set(rows.values_list("result_action_type", flat=True)), {"onsite_web_lead"})

    def test_meta_objective_result_stays_consistent_across_zero_result_days(self):
        sync = self._run(MetaObjectiveResultConnector())

        campaign_rows = InsightDaily.objects.filter(level="campaign").order_by("date")
        self.assertEqual(list(campaign_rows.values_list("results", flat=True)), [Decimal("1"), Decimal("0")])
        self.assertEqual(campaign_rows.first().cost_per_result, Decimal("59.58"))
        self.assertTrue(all(value.startswith("meta_objective:") for value in campaign_rows.values_list("result_action_type", flat=True)))

        dashboard = _dashboard_payload(sync.account, date(2026, 8, 15), date(2026, 8, 16))
        self.assertEqual(dashboard["kpis"][1]["value"], "1.00")
        self.assertEqual(dashboard["campaigns"][0]["results"], "1.00")

    def test_daily_cycle_dispatches_every_active_connection(self):
        second = MetaConnection.objects.create(name="Second", ad_account_external_id="789", is_active=True)
        MetaConnection.objects.create(name="Inactive", ad_account_external_id="999", is_active=False)

        with patch("reporting.tasks.synchronize_meta.delay") as dispatch:
            dispatch.side_effect = [SimpleNamespace(id="task-1"), SimpleNamespace(id="task-2")]
            task_ids = daily_meta_cycle.run()

        self.assertEqual(task_ids, ["task-1", "task-2"])
        self.assertEqual(set(SyncRun.objects.values_list("connection_id", flat=True)), {self.connection.pk, second.pk})
