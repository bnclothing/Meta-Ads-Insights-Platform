import hashlib
import json
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from reporting.models import (
    ActionMetricDaily,
    AdAccount,
    AdSetSnapshot,
    AdSnapshot,
    CampaignSnapshot,
    ConnectionStatus,
    InsightDaily,
    InsightLevel,
    MetaConnection,
    RawApiPayload,
    SyncRun,
    SyncStatus,
)
from reporting.services.alerts import evaluate_alerts


class Command(BaseCommand):
    help = "Populate deterministic, non-personal Meta reporting data for local demonstration and acceptance tests."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=45)
        parser.add_argument("--create-report", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(20260817)
        end = timezone.localdate() - timedelta(days=1)
        start = end - timedelta(days=options["days"] - 1)
        connection, _ = MetaConnection.objects.update_or_create(
            name="Compte Meta ULTEx — Démo",
            defaults={
                "app_id": "demo-app",
                "ad_account_external_id": "1234567890",
                "api_version": "v25.0",
                "status": ConnectionStatus.CONNECTED,
                "is_active": True,
                "last_tested_at": timezone.now(),
                "last_successful_sync_at": timezone.now(),
            },
        )
        account, _ = AdAccount.objects.update_or_create(
            connection=connection,
            external_id="1234567890",
            defaults={
                "name": "ULTEx — Acquisition Maroc",
                "currency": "MAD",
                "timezone_name": "Africa/Casablanca",
                "timezone_offset_hours_utc": Decimal("1"),
                "account_status": 1,
                "last_synced_at": timezone.now(),
                "raw_data": {"demo": True},
            },
        )
        campaigns_config = [
            ("cmp-1", "Prospection — Formulaire FR", "OUTCOME_LEADS", Decimal("430"), Decimal("34")),
            ("cmp-2", "Retargeting — Marrakech", "OUTCOME_LEADS", Decimal("255"), Decimal("19")),
            ("cmp-3", "Services — Audience large", "OUTCOME_LEADS", Decimal("190"), Decimal("10")),
        ]
        campaigns = {}
        adsets = {}
        ads = {}
        for campaign_id, name, objective, _, _ in campaigns_config:
            campaign, _ = CampaignSnapshot.objects.update_or_create(
                account=account,
                external_id=campaign_id,
                defaults={"name": name, "objective": objective, "status": "ACTIVE", "effective_status": "ACTIVE", "daily_budget": Decimal("500"), "raw_data": {"demo": True}},
            )
            campaigns[campaign_id] = campaign
            adset_id = campaign_id.replace("cmp", "set")
            adset, _ = AdSetSnapshot.objects.update_or_create(
                account=account,
                external_id=adset_id,
                defaults={
                    "campaign": campaign,
                    "campaign_external_id": campaign_id,
                    "name": f"Ensemble — {name.split('—')[0].strip()}",
                    "optimization_goal": "LEAD_GENERATION",
                    "billing_event": "IMPRESSIONS",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                    "raw_data": {"demo": True},
                },
            )
            adsets[adset_id] = adset
            for index in (1, 2):
                ad_id = f"{campaign_id}-ad-{index}"
                ad, _ = AdSnapshot.objects.update_or_create(
                    account=account,
                    external_id=ad_id,
                    defaults={
                        "campaign": campaign,
                        "adset": adset,
                        "campaign_external_id": campaign_id,
                        "adset_external_id": adset_id,
                        "name": f"{name} — Créatif {index}",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                        "raw_data": {"demo": True},
                    },
                )
                ads[ad_id] = ad

        sync_run, _ = SyncRun.objects.update_or_create(
            connection=connection,
            requested_start=start,
            requested_end=end,
            trigger="demo",
            defaults={
                "account": account,
                "status": SyncStatus.SUCCESS,
                "levels": list(InsightLevel.values),
                "started_at": timezone.now() - timedelta(minutes=7),
                "finished_at": timezone.now(),
                "message": "Données de démonstration synchronisées.",
            },
        )
        payload_body = {"demo": True, "period": {"start": start.isoformat(), "end": end.isoformat()}, "notice": "No personal or production data."}
        raw, _ = RawApiPayload.objects.update_or_create(
            sync_run=sync_run,
            endpoint="demo://meta/insights",
            page_number=1,
            defaults={
                "request_params": {"level": "all", "time_increment": 1},
                "payload": payload_body,
                "checksum": hashlib.sha256(json.dumps(payload_body, sort_keys=True).encode()).hexdigest(),
            },
        )

        row_count = 0
        for day_index in range(options["days"]):
            day = start + timedelta(days=day_index)
            campaign_metrics = []
            for campaign_index, (campaign_id, name, _, base_spend, base_results) in enumerate(campaigns_config):
                weekday_factor = Decimal("0.90") if day.weekday() >= 5 else Decimal("1.00")
                growth = Decimal("1") + Decimal(day_index) / Decimal("500")
                noise = Decimal(str(rng.uniform(0.88, 1.12)))
                spend = (base_spend * weekday_factor * growth * noise).quantize(Decimal("0.01"))
                result_noise = Decimal(str(rng.uniform(0.82, 1.18)))
                results = (base_results * weekday_factor * growth * result_noise).quantize(Decimal("1"))
                if day == end and campaign_index == 2:
                    spend = Decimal("408.00")
                    results = Decimal("8")
                impressions = int(spend * Decimal("72") + rng.randint(500, 1600))
                reach = int(impressions * rng.uniform(.67, .79))
                clicks = int(impressions * rng.uniform(.012, .021))
                link_clicks = int(clicks * .78)
                cpr = (spend / results).quantize(Decimal("0.000001")) if results else None
                ctr = (Decimal(clicks) / Decimal(impressions) * 100).quantize(Decimal("0.000001"))
                cpc = (spend / Decimal(clicks)).quantize(Decimal("0.000001")) if clicks else None
                cpm = (spend / Decimal(impressions) * 1000).quantize(Decimal("0.000001"))
                frequency = (Decimal(impressions) / Decimal(reach)).quantize(Decimal("0.000001")) if reach else None
                campaign_metrics.append((campaign_id, name, spend, results, impressions, reach, clicks, link_clicks, frequency, ctr, cpc, cpm, cpr))

            total_spend = sum((item[2] for item in campaign_metrics), Decimal("0"))
            total_results = sum((item[3] for item in campaign_metrics), Decimal("0"))
            total_impressions = sum(item[4] for item in campaign_metrics)
            total_reach = sum(item[5] for item in campaign_metrics)
            total_clicks = sum(item[6] for item in campaign_metrics)
            total_links = sum(item[7] for item in campaign_metrics)
            levels = [(InsightLevel.ACCOUNT, account.external_id, account.name, "", total_spend, total_results, total_impressions, total_reach, total_clicks, total_links)]
            for item in campaign_metrics:
                levels.append((InsightLevel.CAMPAIGN, item[0], item[1], item[0], item[2], item[3], item[4], item[5], item[6], item[7]))
                adset_id = item[0].replace("cmp", "set")
                levels.append((InsightLevel.ADSET, adset_id, adsets[adset_id].name, item[0], item[2], item[3], item[4], item[5], item[6], item[7]))
                for index in (1, 2):
                    ad_id = f"{item[0]}-ad-{index}"
                    ratio = Decimal("0.56") if index == 1 else Decimal("0.44")
                    levels.append((InsightLevel.AD, ad_id, ads[ad_id].name, item[0], item[2] * ratio, (item[3] * ratio).quantize(Decimal("0.01")), int(item[4] * float(ratio)), int(item[5] * float(ratio)), int(item[6] * float(ratio)), int(item[7] * float(ratio))))

            for level, object_id, name, campaign_id, spend, results, impressions, reach, clicks, link_clicks in levels:
                cpr = (spend / results).quantize(Decimal("0.000001")) if results else None
                ctr = (Decimal(clicks) / Decimal(impressions) * 100).quantize(Decimal("0.000001")) if impressions else None
                insight, _ = InsightDaily.objects.update_or_create(
                    account=account,
                    date=day,
                    level=level,
                    object_external_id=object_id,
                    attribution_key="demo-unified",
                    defaults={
                        "sync_run": sync_run,
                        "raw_payload": raw,
                        "object_name": name,
                        "campaign_external_id": campaign_id,
                        "adset_external_id": object_id if level == InsightLevel.ADSET else (object_id.split("-ad-")[0].replace("cmp", "set") if level == InsightLevel.AD else ""),
                        "ad_external_id": object_id if level == InsightLevel.AD else "",
                        "currency": "MAD",
                        "spend": spend,
                        "results": results,
                        "cost_per_result": cpr,
                        "impressions": impressions,
                        "reach": reach,
                        "clicks": clicks,
                        "link_clicks": link_clicks,
                        "frequency": (Decimal(impressions) / Decimal(reach)).quantize(Decimal("0.000001")) if reach else None,
                        "ctr": ctr,
                        "cpc": (spend / Decimal(clicks)).quantize(Decimal("0.000001")) if clicks else None,
                        "cpm": (spend / Decimal(impressions) * 1000).quantize(Decimal("0.000001")) if impressions else None,
                        "result_action_type": "onsite_conversion.lead_grouped",
                        "result_label": "Meta résultat",
                        "result_verified": False,
                        "attribution_setting": {"action_report_time": "impression", "use_unified_attribution_setting": True},
                        "actions": [{"action_type": "onsite_conversion.lead_grouped", "value": str(results)}],
                        "cost_per_action_type": [{"action_type": "onsite_conversion.lead_grouped", "value": str(cpr)}] if cpr else [],
                        "fetched_at": timezone.now(),
                    },
                )
                ActionMetricDaily.objects.update_or_create(
                    insight=insight,
                    action_type="onsite_conversion.lead_grouped",
                    defaults={"value": results, "cost": cpr},
                )
                row_count += 1
            evaluate_alerts(account, day)

        sync_run.records_count = row_count
        sync_run.raw_pages_count = 1
        sync_run.save(update_fields=["records_count", "raw_pages_count", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"Seeded {row_count} daily insight rows from {start} to {end}."))
        if options["create_report"]:
            from reporting.services.reports import generate_report

            user = get_user_model().objects.order_by("id").first()
            version = generate_report(account, end, end, source="demo", user=user)
            self.stdout.write(self.style.SUCCESS(f"Generated demo report version {version.pk}."))

