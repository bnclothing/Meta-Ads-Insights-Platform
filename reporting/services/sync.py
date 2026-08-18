from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone as datetime_timezone
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from reporting.connectors import MetaApiError, MetaMarketingConnector
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
    MetricMapping,
    RawApiPayload,
    SyncError,
    SyncRun,
    SyncStatus,
)

from .alerts import evaluate_alerts
from .metrics import ZERO, decimal_value, int_value, safe_divide


PREFERRED_ACTION_TYPES = [
    "offsite_conversion.fb_pixel_lead",
    "onsite_conversion.lead_grouped",
    "onsite_conversion.messaging_conversation_started_7d",
    "onsite_conversion.messaging_first_reply",
    "lead",
]


def _action_stats_by_type(value, *, default_action_type=""):
    if not isinstance(value, list):
        return {}
    stats = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        action_type = item.get("action_type") or (default_action_type if len(value) == 1 else "")
        metric_value = decimal_value(item.get("value"), None)
        if action_type and metric_value is not None:
            stats[action_type] = metric_value
    return stats


def _select_single_action(stats, costs, *, predicate):
    candidates = [(action_type, value) for action_type, value in stats.items() if predicate(action_type)]
    if len(candidates) != 1:
        return None
    action_type, value = candidates[0]
    return value, costs.get(action_type), action_type


def _single_meta_metric(value):
    """Extract one unambiguous numeric value from a Meta metric object."""
    if value in (None, "", []):
        return None
    if not isinstance(value, (list, dict)):
        return decimal_value(value, None)

    found = []

    def collect(item):
        if isinstance(item, list):
            for child in item:
                collect(child)
        elif isinstance(item, dict):
            if "value" in item and not isinstance(item["value"], (list, dict)):
                parsed = decimal_value(item["value"], None)
                if parsed is not None:
                    found.append(parsed)
            elif "values" in item:
                collect(item["values"])

    collect(value)
    unique = list(dict.fromkeys(found))
    return unique[0] if len(unique) == 1 else None


def _meta_metric_indicator(value):
    items = value if isinstance(value, list) else [value]
    indicators = {
        str(item.get("indicator") or item.get("action_type"))
        for item in items
        if isinstance(item, dict) and (item.get("indicator") or item.get("action_type"))
    }
    return next(iter(indicators)) if len(indicators) == 1 else "primary_result"


def _meta_result_scopes(insight_pages):
    account_available = False
    campaign_ids = set()
    for level, pages in insight_pages.items():
        for page in pages:
            for row in page.payload.get("data", []):
                if _single_meta_metric(row.get("cost_per_result")) is None:
                    continue
                if level == InsightLevel.ACCOUNT:
                    account_available = True
                elif row.get("campaign_id"):
                    campaign_ids.add(str(row["campaign_id"]))
    return account_available, campaign_ids


def _parse_dt(value):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def _budget(value):
    amount = decimal_value(value, None)
    return amount / Decimal("100") if amount is not None else None


def _checksum(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _store_page(sync_run, page):
    return RawApiPayload.objects.create(
        sync_run=sync_run,
        endpoint=page.endpoint[:300],
        request_params=page.request_params,
        payload=page.payload,
        page_number=page.page_number,
        checksum=_checksum(page.payload),
    )


def _select_result(row, connection, mapping_cache, campaign_objective="", *, prefer_meta_result=False):
    campaign_id = row.get("campaign_id", "")
    candidates = [
        ("campaign", campaign_id),
        ("objective", campaign_objective),
        ("global", "*"),
    ]
    actions = _action_stats_by_type(row.get("actions", []))
    costs = _action_stats_by_type(row.get("cost_per_action_type", []))

    for scope in candidates:
        mapping = mapping_cache.get(scope)
        if mapping and mapping.is_active:
            value = actions.get(mapping.action_type, ZERO)
            cost = costs.get(mapping.action_type)
            return value, cost or safe_divide(decimal_value(row.get("spend"), ZERO), value), mapping.action_type, mapping.label, mapping.is_verified

    if prefer_meta_result:
        cost = _single_meta_metric(row.get("cost_per_result"))
        indicator = _meta_metric_indicator(row.get("cost_per_result"))
        action_type = f"meta_objective:{indicator}"[:180]
        if cost is None:
            return ZERO, None, action_type, "Meta résultat", False
        value = safe_divide(decimal_value(row.get("spend"), ZERO), cost)
        return value, cost, action_type, "Meta résultat", False

    for action_type in PREFERRED_ACTION_TYPES:
        if action_type in actions:
            value = actions[action_type]
            cost = costs.get(action_type) or safe_divide(decimal_value(row.get("spend"), ZERO), value)
            return value, cost, action_type, "Meta résultat", False

    # Meta occasionally exposes a new lead action name before it is added to
    # the documented aliases. Use it only when exactly one lead-like action is
    # present, so overlapping action types are never added or guessed between.
    lead_action = _select_single_action(actions, costs, predicate=lambda action_type: "lead" in action_type.lower())
    if lead_action:
        value, cost, action_type = lead_action
        return value, cost or safe_divide(decimal_value(row.get("spend"), ZERO), value), action_type, "Meta résultat", False

    # `conversion_leads` is a dedicated Ads Insights field and is separate
    # from the generic `actions` array. The connector has always requested it;
    # normalize a single unambiguous value instead of silently discarding it.
    conversion_leads = _action_stats_by_type(row.get("conversion_leads", []), default_action_type="conversion_lead")
    conversion_costs = _action_stats_by_type(row.get("cost_per_conversion_lead", []), default_action_type="conversion_lead")
    conversion_lead = _select_single_action(conversion_leads, conversion_costs, predicate=lambda _action_type: True)
    if conversion_lead:
        value, cost, action_type = conversion_lead
        source = f"conversion_leads:{action_type}"
        return value, cost or safe_divide(decimal_value(row.get("spend"), ZERO), value), source, "Meta résultat", False
    return None, None, "", "Meta résultat", False


def _mapping_cache(connection):
    return {
        (mapping.scope_type, mapping.scope_value): mapping
        for mapping in MetricMapping.objects.filter(connection=connection, is_active=True)
    }


def _upsert_account(connection, details):
    external_id = str(details.get("account_id") or connection.ad_account_external_id).removeprefix("act_")
    account, _ = AdAccount.objects.update_or_create(
        connection=connection,
        external_id=external_id,
        defaults={
            "name": details.get("name", ""),
            "currency": details.get("currency", "MAD"),
            "timezone_name": details.get("timezone_name", "Africa/Casablanca"),
            "timezone_offset_hours_utc": decimal_value(details.get("timezone_offset_hours_utc"), ZERO),
            "account_status": details.get("account_status"),
            "raw_data": details,
            "last_synced_at": timezone.now(),
        },
    )
    return account


def _upsert_objects(account, object_pages):
    campaigns = {}
    for page in object_pages.get("campaigns", []):
        for item in page.payload.get("data", []):
            obj, _ = CampaignSnapshot.objects.update_or_create(
                account=account,
                external_id=str(item["id"]),
                defaults={
                    "name": item.get("name", ""),
                    "objective": item.get("objective", ""),
                    "buying_type": item.get("buying_type", ""),
                    "status": item.get("status", ""),
                    "effective_status": item.get("effective_status", ""),
                    "daily_budget": _budget(item.get("daily_budget")),
                    "lifetime_budget": _budget(item.get("lifetime_budget")),
                    "start_time": _parse_dt(item.get("start_time")),
                    "stop_time": _parse_dt(item.get("stop_time")),
                    "raw_data": item,
                    "last_seen_at": timezone.now(),
                },
            )
            campaigns[obj.external_id] = obj
    adsets = {}
    for page in object_pages.get("adsets", []):
        for item in page.payload.get("data", []):
            campaign_id = str(item.get("campaign_id", ""))
            obj, _ = AdSetSnapshot.objects.update_or_create(
                account=account,
                external_id=str(item["id"]),
                defaults={
                    "campaign": campaigns.get(campaign_id),
                    "campaign_external_id": campaign_id,
                    "name": item.get("name", ""),
                    "optimization_goal": item.get("optimization_goal", ""),
                    "billing_event": item.get("billing_event", ""),
                    "status": item.get("status", ""),
                    "effective_status": item.get("effective_status", ""),
                    "daily_budget": _budget(item.get("daily_budget")),
                    "lifetime_budget": _budget(item.get("lifetime_budget")),
                    "start_time": _parse_dt(item.get("start_time")),
                    "stop_time": _parse_dt(item.get("end_time")),
                    "raw_data": item,
                    "last_seen_at": timezone.now(),
                },
            )
            adsets[obj.external_id] = obj
    for page in object_pages.get("ads", []):
        for item in page.payload.get("data", []):
            campaign_id = str(item.get("campaign_id", ""))
            adset_id = str(item.get("adset_id", ""))
            creative = item.get("creative") or {}
            AdSnapshot.objects.update_or_create(
                account=account,
                external_id=str(item["id"]),
                defaults={
                    "campaign": campaigns.get(campaign_id),
                    "adset": adsets.get(adset_id),
                    "campaign_external_id": campaign_id,
                    "adset_external_id": adset_id,
                    "creative_external_id": str(creative.get("id", "")),
                    "name": item.get("name", ""),
                    "status": item.get("status", ""),
                    "effective_status": item.get("effective_status", ""),
                    "raw_data": item,
                    "last_seen_at": timezone.now(),
                },
            )
    return campaigns


def _upsert_insight(
    account,
    connection,
    sync_run,
    raw_page,
    level,
    row,
    mapping_cache,
    campaigns,
    *,
    account_meta_result_available=False,
    meta_result_campaign_ids=None,
):
    level_id_fields = {
        InsightLevel.ACCOUNT: ("account_id", "account_name"),
        InsightLevel.CAMPAIGN: ("campaign_id", "campaign_name"),
        InsightLevel.ADSET: ("adset_id", "adset_name"),
        InsightLevel.AD: ("ad_id", "ad_name"),
    }
    id_field, name_field = level_id_fields[level]
    object_id = str(row.get(id_field) or account.external_id)
    campaign_id = str(row.get("campaign_id", ""))
    objective = campaigns.get(campaign_id).objective if campaigns.get(campaign_id) else ""
    prefer_meta_result = account_meta_result_available if level == InsightLevel.ACCOUNT else campaign_id in (meta_result_campaign_ids or set())
    results, cpr, action_type, result_label, verified = _select_result(
        row,
        connection,
        mapping_cache,
        objective,
        prefer_meta_result=prefer_meta_result,
    )
    attribution = {
        "action_report_time": "impression",
        "use_unified_attribution_setting": True,
        "source": "Meta Marketing API",
    }
    attribution_key = hashlib.sha256(json.dumps(attribution, sort_keys=True).encode("utf-8")).hexdigest()[:32]
    insight, _ = InsightDaily.objects.update_or_create(
        account=account,
        date=date.fromisoformat(row.get("date_start")),
        level=level,
        object_external_id=object_id,
        attribution_key=attribution_key,
        defaults={
            "sync_run": sync_run,
            "raw_payload": raw_page,
            "object_name": row.get(name_field, "") or account.name,
            "campaign_external_id": campaign_id,
            "adset_external_id": str(row.get("adset_id", "")),
            "ad_external_id": str(row.get("ad_id", "")),
            "currency": row.get("account_currency") or account.currency,
            "spend": decimal_value(row.get("spend"), ZERO),
            "impressions": int_value(row.get("impressions")),
            "reach": int_value(row.get("reach")),
            "clicks": int_value(row.get("clicks")),
            "link_clicks": int_value(row.get("inline_link_clicks")),
            "frequency": decimal_value(row.get("frequency"), None),
            "ctr": decimal_value(row.get("ctr"), None),
            "cpc": decimal_value(row.get("cpc"), None),
            "cpm": decimal_value(row.get("cpm"), None),
            "results": results,
            "cost_per_result": cpr,
            "result_action_type": action_type,
            "result_label": result_label,
            "result_verified": verified,
            "attribution_setting": attribution,
            "actions": row.get("actions", []),
            "cost_per_action_type": row.get("cost_per_action_type", []),
            "fetched_at": timezone.now(),
        },
    )
    ActionMetricDaily.objects.filter(insight=insight).delete()
    costs = {item.get("action_type"): decimal_value(item.get("value"), None) for item in row.get("cost_per_action_type", [])}
    ActionMetricDaily.objects.bulk_create(
        [
            ActionMetricDaily(
                insight=insight,
                action_type=item.get("action_type", "unknown"),
                value=decimal_value(item.get("value"), ZERO),
                cost=costs.get(item.get("action_type")),
            )
            for item in row.get("actions", [])
            if item.get("action_type")
        ]
    )
    return insight


def perform_sync(sync_run: SyncRun, *, connector=None) -> SyncRun:
    connection = sync_run.connection
    connector = connector or MetaMarketingConnector(connection)
    sync_run.status = SyncStatus.RUNNING
    sync_run.started_at = sync_run.started_at or timezone.now()
    sync_run.message = "Connexion à Meta…"
    sync_run.save(update_fields=["status", "started_at", "message", "updated_at"])

    try:
        health = connector.test_connection()
        object_pages = connector.sync_objects()
        insight_pages: dict[str, list] = {}
        level_errors: list[tuple[str, MetaApiError]] = []
        for level in sync_run.levels:
            try:
                insight_pages.update(connector.sync_insights(sync_run.requested_start, sync_run.requested_end, [level]))
            except MetaApiError as exc:
                level_errors.append((level, exc))

        retryable_errors = [exc for _, exc in level_errors if exc.retryable]
        if retryable_errors:
            raise retryable_errors[0]

        with transaction.atomic():
            account = _upsert_account(connection, health.details)
            sync_run.account = account
            raw_page_lookup = {}
            for group_pages in list(object_pages.values()) + list(insight_pages.values()):
                for page in group_pages:
                    raw = _store_page(sync_run, page)
                    raw_page_lookup[(page.endpoint, page.page_number, _checksum(page.payload))] = raw
            campaigns = _upsert_objects(account, object_pages)
            mappings = _mapping_cache(connection)
            account_meta_result_available, meta_result_campaign_ids = _meta_result_scopes(insight_pages)
            record_count = 0
            for level, pages in insight_pages.items():
                for page in pages:
                    raw = raw_page_lookup[(page.endpoint, page.page_number, _checksum(page.payload))]
                    for row in page.payload.get("data", []):
                        _upsert_insight(
                            account,
                            connection,
                            sync_run,
                            raw,
                            level,
                            row,
                            mappings,
                            campaigns,
                            account_meta_result_available=account_meta_result_available,
                            meta_result_campaign_ids=meta_result_campaign_ids,
                        )
                        record_count += 1
            for level, exc in level_errors:
                SyncError.objects.create(
                    sync_run=sync_run,
                    level=level,
                    code=exc.code,
                    subcode=exc.subcode,
                    message=str(exc),
                    user_message=exc.user_message,
                    is_retryable=exc.retryable,
                )
            sync_run.records_count = record_count
            sync_run.raw_pages_count = len(raw_page_lookup)
            sync_run.status = SyncStatus.PARTIAL if level_errors else SyncStatus.SUCCESS
            sync_run.message = "Synchronisation partielle : certains niveaux sont indisponibles." if level_errors else "Synchronisation terminée."
            sync_run.finished_at = timezone.now()
            sync_run.save()
            connection.status = ConnectionStatus.ERROR if level_errors else ConnectionStatus.CONNECTED
            connection.last_tested_at = timezone.now()
            connection.last_error = "; ".join(f"{level}: {exc}" for level, exc in level_errors)
            if not level_errors:
                connection.last_successful_sync_at = timezone.now()
            connection.save()

        if sync_run.status == SyncStatus.SUCCESS:
            day = sync_run.requested_start
            while day <= sync_run.requested_end:
                evaluate_alerts(sync_run.account, day)
                day = date.fromordinal(day.toordinal() + 1)
        return sync_run
    except MetaApiError as exc:
        SyncError.objects.create(
            sync_run=sync_run,
            code=exc.code,
            subcode=exc.subcode,
            message=str(exc),
            user_message=exc.user_message,
            is_retryable=exc.retryable,
        )
        sync_run.status = SyncStatus.FAILED
        sync_run.finished_at = timezone.now()
        sync_run.message = exc.user_message or str(exc)
        sync_run.save(update_fields=["status", "finished_at", "message", "updated_at"])
        connection.status = ConnectionStatus.ERROR
        connection.last_tested_at = timezone.now()
        connection.last_error = sync_run.message
        connection.save(update_fields=["status", "last_tested_at", "last_error", "updated_at"])
        raise
    except Exception as exc:
        SyncError.objects.create(sync_run=sync_run, code="internal", message=str(exc), is_retryable=False)
        sync_run.status = SyncStatus.FAILED
        sync_run.finished_at = timezone.now()
        sync_run.message = "La synchronisation a échoué pendant le traitement des données."
        sync_run.save(update_fields=["status", "finished_at", "message", "updated_at"])
        connection.status = ConnectionStatus.ERROR
        connection.last_error = sync_run.message
        connection.save(update_fields=["status", "last_error", "updated_at"])
        raise
