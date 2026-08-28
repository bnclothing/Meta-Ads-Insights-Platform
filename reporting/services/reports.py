from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from reporting.models import (
    Anomaly,
    AnomalyStatus,
    InsightDaily,
    InsightLevel,
    ReportRun,
    ReportScope,
    ReportStatus,
    ReportVersion,
    Severity,
    SyncRun,
)

from .exports import build_excel, build_pdf
from .metrics import ZERO, aggregate_rows, average, percent_change, safe_divide


def _decimal(value, places="0.01"):
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal(places)))


def _row_summary(row):
    return {
        "id": row.pk,
        "date": row.date.isoformat(),
        "level": row.level,
        "object_id": row.object_external_id,
        "name": row.object_name,
        "currency": row.currency,
        "spend": _decimal(row.spend),
        "results": _decimal(row.results),
        "cost_per_result": _decimal(row.cost_per_result),
        "impressions": row.impressions,
        "reach": row.reach,
        "clicks": row.clicks,
        "link_clicks": row.link_clicks,
        "frequency": _decimal(row.frequency),
        "ctr": _decimal(row.ctr),
        "cpc": _decimal(row.cpc),
        "cpm": _decimal(row.cpm),
        "result_action_type": row.result_action_type,
        "result_label": row.result_label,
        "result_verified": row.result_verified,
        "raw_payload_id": row.raw_payload_id,
        "sync_run_id": row.sync_run_id,
        "account_id": row.account_id,
        "account_external_id": row.account.external_id,
        "account_name": row.account.name,
        "insight_ids": [row.pk],
        "raw_payload_ids": [row.raw_payload_id] if row.raw_payload_id else [],
    }


def _group_objects(rows, *, include_account=False):
    grouped = defaultdict(list)
    for row in rows:
        grouped[
            (
                row.account_id if include_account else None,
                row.object_external_id,
                row.object_name,
                row.result_action_type,
                row.result_label,
                row.result_verified,
            )
        ].append(row)
    output = []
    for (account_id, object_id, name, action_type, label, verified), members in grouped.items():
        metrics = aggregate_rows(members)
        account = members[0].account
        output.append(
            {
                "account_id": account_id,
                "account_external_id": account.external_id,
                "account_name": account.name,
                "object_id": object_id,
                "name": name,
                "currency": members[0].currency,
                "spend": _decimal(metrics["spend"]),
                "results": _decimal(metrics["results"]),
                "cost_per_result": _decimal(metrics["cost_per_result"]),
                "impressions": metrics["impressions"],
                "reach": metrics["reach"],
                "clicks": metrics["clicks"],
                "ctr": _decimal(metrics["ctr"]),
                "cpc": _decimal(metrics["cpc"]),
                "cpm": _decimal(metrics["cpm"]),
                "result_action_type": action_type,
                "result_label": label,
                "result_verified": verified,
                "insight_ids": [item.pk for item in members],
                "raw_payload_ids": sorted({item.raw_payload_id for item in members if item.raw_payload_id}),
            }
        )
    output.sort(key=lambda item: (item["results"] is not None, Decimal(item["results"] or "0"), -Decimal(item["spend"] or "0")), reverse=True)
    return output


def _cost_ranking(records):
    eligible = [item for item in records if item["cost_per_result"] is not None and Decimal(item["spend"] or "0") > 0]
    if not eligible:
        return {"strongest": None, "weakest": None, "basis": "cost_per_result"}

    def compact(item):
        return {
            "object_id": item["object_id"],
            "name": item["name"],
            "spend": item["spend"],
            "results": item["results"],
            "cost_per_result": item["cost_per_result"],
        }

    return {
        "strongest": compact(min(eligible, key=lambda item: Decimal(item["cost_per_result"]))),
        "weakest": compact(max(eligible, key=lambda item: Decimal(item["cost_per_result"]))),
        "basis": "cost_per_result",
    }


def _comparison(account, current_row, lookback_days):
    if not current_row:
        return {"days": lookback_days, "available": False}
    history = list(
        InsightDaily.objects.filter(
            account=account,
            level=InsightLevel.ACCOUNT,
            date__gte=current_row.date - timedelta(days=lookback_days),
            date__lt=current_row.date,
        ).order_by("date")
    )
    if not history:
        return {"days": lookback_days, "available": False}
    baseline_spend = average(item.spend for item in history)
    results_available = current_row.results is not None and all(item.results is not None for item in history)
    baseline_results = average(item.results for item in history) if results_available else None
    current_results = current_row.results if results_available else None
    baseline_cpl = (
        safe_divide(sum((item.spend for item in history), ZERO), sum((item.results for item in history), ZERO))
        if results_available
        else None
    )
    current_cpl = safe_divide(current_row.spend, current_results) if results_available else None
    return {
        "days": lookback_days,
        "available": True,
        "sample_days": len(history),
        "spend_average": _decimal(baseline_spend),
        "results_average": _decimal(baseline_results),
        "cpl_average": _decimal(baseline_cpl),
        "spend_change_percent": _decimal(percent_change(current_row.spend, baseline_spend)),
        "results_change_percent": _decimal(percent_change(current_results, baseline_results)) if results_available else None,
        "cpl_change_percent": _decimal(percent_change(current_cpl, baseline_cpl)) if current_cpl is not None and baseline_cpl is not None else None,
    }


def _known_result_total(rows):
    values = [row.results for row in rows if row.results is not None]
    return sum(values, ZERO) if values else None


def _portfolio_comparison(accounts, current_date, lookback_days, *, mixed_currency=False):
    history_start = current_date - timedelta(days=lookback_days)
    account_rows = list(
        InsightDaily.objects.filter(
            account__in=accounts,
            level=InsightLevel.ACCOUNT,
            date__range=(history_start, current_date),
        ).order_by("date", "account_id")
    )
    campaign_rows = list(
        InsightDaily.objects.filter(
            account__in=accounts,
            level=InsightLevel.CAMPAIGN,
            date__range=(history_start, current_date),
        ).order_by("date", "account_id")
    )
    account_by_date = defaultdict(list)
    campaigns_by_date = defaultdict(list)
    for row in account_rows:
        account_by_date[row.date].append(row)
    for row in campaign_rows:
        campaigns_by_date[row.date].append(row)
    current_rows = account_by_date.get(current_date, [])
    history_dates = sorted(day for day in account_by_date if day < current_date)
    if not current_rows or not history_dates:
        return {"days": lookback_days, "available": False}

    current_metrics = aggregate_rows(current_rows)
    history_metrics = [aggregate_rows(account_by_date[day]) for day in history_dates]
    current_results = _known_result_total(campaigns_by_date[current_date])
    history_results = [_known_result_total(campaigns_by_date[day]) for day in history_dates]
    results_available = current_results is not None and all(value is not None for value in history_results)
    baseline_results = average(history_results) if results_available else None
    baseline_spend = average(item["spend"] for item in history_metrics)
    current_spend = current_metrics["spend"]
    baseline_cpl = safe_divide(baseline_spend, baseline_results) if results_available and not mixed_currency else None
    current_cpl = safe_divide(current_spend, current_results) if results_available and not mixed_currency else None
    return {
        "days": lookback_days,
        "available": True,
        "sample_days": len(history_dates),
        "spend_average": None if mixed_currency else _decimal(baseline_spend),
        "results_average": _decimal(baseline_results),
        "cpl_average": _decimal(baseline_cpl),
        "spend_change_percent": None if mixed_currency else _decimal(percent_change(current_spend, baseline_spend)),
        "results_change_percent": _decimal(percent_change(current_results, baseline_results)) if results_available else None,
        "cpl_change_percent": _decimal(percent_change(current_cpl, baseline_cpl)) if current_cpl is not None and baseline_cpl is not None else None,
    }


def _portfolio_trend(account_rows, campaign_rows, *, mixed_currency=False):
    accounts_by_date = defaultdict(list)
    campaigns_by_date = defaultdict(list)
    for row in account_rows:
        accounts_by_date[row.date].append(row)
    for row in campaign_rows:
        campaigns_by_date[row.date].append(row)
    trend = []
    for day, rows in sorted(accounts_by_date.items()):
        metrics = aggregate_rows(rows)
        results = _known_result_total(campaigns_by_date[day])
        trend.append(
            {
                "id": None,
                "date": day.isoformat(),
                "level": InsightLevel.ACCOUNT,
                "object_id": "portfolio",
                "name": "Tous les comptes Meta",
                "currency": "" if mixed_currency else rows[0].currency,
                "spend": None if mixed_currency else _decimal(metrics["spend"]),
                "results": _decimal(results),
                "cost_per_result": None if mixed_currency or results is None else _decimal(safe_divide(metrics["spend"], results)),
                "impressions": metrics["impressions"],
                "reach": metrics["reach"],
                "clicks": metrics["clicks"],
                "link_clicks": metrics["link_clicks"],
                "frequency": None,
                "ctr": _decimal(metrics["ctr"]),
                "cpc": None if mixed_currency else _decimal(metrics["cpc"]),
                "cpm": None if mixed_currency else _decimal(metrics["cpm"]),
                "result_action_type": "meta_portfolio_result",
                "result_label": "Meta résultats",
                "result_verified": all(row.result_verified for row in campaigns_by_date[day]),
                "raw_payload_id": None,
                "sync_run_id": None,
                "account_id": None,
                "account_external_id": "portfolio",
                "account_name": "Tous les comptes Meta",
                "insight_ids": [row.pk for row in rows],
                "raw_payload_ids": sorted({row.raw_payload_id for row in rows if row.raw_payload_id}),
            }
        )
    max_spend = max((Decimal(item["spend"] or "0") for item in trend), default=ZERO)
    result_values = [Decimal(item["results"]) for item in trend if item["results"] is not None]
    max_results = max(result_values, default=ZERO)
    for item in trend:
        item["chart_spend_percent"] = int((Decimal(item["spend"] or "0") / max_spend) * 100) if max_spend else 0
        item["chart_results_percent"] = (
            int((Decimal(item["results"]) / max_results) * 100) if item["results"] is not None and max_results else None
        )
    return trend


def _executive_summary(kpis, comparisons, anomalies, currency, data_quality, *, mixed_currency=False):
    if not kpis["has_data"]:
        return "Aucune donnée Meta complète n’est disponible pour la période sélectionnée. Vérifiez la synchronisation avant toute interprétation."
    spend = Decimal(kpis["spend"] or "0")
    cpl = kpis["cost_per_result"]
    if mixed_currency:
        if kpis["results"] is None:
            parts = ["Les comptes utilisent plusieurs devises; les dépenses et le coût par résultat ne sont pas additionnés. La mesure de résultat consolidée reste indisponible."]
        else:
            results = Decimal(kpis["results"])
            parts = [f"Meta a enregistré {results.quantize(Decimal('0.01'))} résultat(s) sur l’ensemble des comptes. Les dépenses ne sont pas additionnées car plusieurs devises sont présentes."]
    elif kpis["results"] is None:
        parts = [f"Meta n’a pas fourni de mesure de résultat complète pour {spend.quantize(Decimal('0.01'))} {currency} dépensés; aucun zéro n’est imputé."]
    else:
        results = Decimal(kpis["results"])
        parts = [f"Meta a enregistré {results.quantize(Decimal('0.01'))} résultat(s) pour {spend.quantize(Decimal('0.01'))} {currency} dépensés."]
    if cpl is not None:
        parts.append(f"Le coût par résultat calculé est de {Decimal(cpl).quantize(Decimal('0.01'))} {currency}.")
    seven = comparisons.get("7", {})
    if seven.get("available") and seven.get("results_change_percent") is not None:
        change = Decimal(seven["results_change_percent"])
        direction = "supérieurs" if change >= 0 else "inférieurs"
        parts.append(f"Les résultats sont {direction} de {abs(change).quantize(Decimal('0.1'))} % à la moyenne des sept jours précédents.")
    blocking = sum(1 for item in anomalies if item["severity"] in {"critical", "high"})
    if blocking:
        parts.append(f"{blocking} alerte(s) critique(s) ou élevée(s) nécessitent une vérification humaine.")
    else:
        parts.append("Aucune alerte critique ou élevée n’est ouverte pour cette période.")
    if data_quality:
        parts.append("Des réserves de qualité des données sont détaillées dans le rapport.")
    return " ".join(parts)


def build_snapshot(account, date_start: date, date_end: date):
    account_rows = list(
        InsightDaily.objects.select_related("account").filter(account=account, level=InsightLevel.ACCOUNT, date__range=(date_start, date_end)).order_by("date")
    )
    current_row = account_rows[-1] if account_rows else None
    aggregate = aggregate_rows(account_rows)
    currency = current_row.currency if current_row else account.currency
    kpis = {
        "has_data": bool(account_rows),
        "spend": _decimal(aggregate["spend"]),
        "results": _decimal(aggregate["results"]),
        "cost_per_result": _decimal(aggregate["cost_per_result"]),
        "impressions": aggregate["impressions"],
        "reach_cumulative": aggregate["reach"],
        "clicks": aggregate["clicks"],
        "link_clicks": aggregate["link_clicks"],
        "ctr": _decimal(aggregate["ctr"]),
        "cpc": _decimal(aggregate["cpc"]),
        "cpm": _decimal(aggregate["cpm"]),
    }
    comparisons = {
        "1": _comparison(account, current_row, 1),
        "7": _comparison(account, current_row, 7),
        "30": _comparison(account, current_row, 30),
    }
    trend_start = min(date_start, date_end - timedelta(days=13))
    trend_rows = InsightDaily.objects.filter(
        account=account, level=InsightLevel.ACCOUNT, date__range=(trend_start, date_end)
    ).select_related("account").order_by("date")
    trend = [_row_summary(row) for row in trend_rows]
    max_spend = max((Decimal(item["spend"] or "0") for item in trend), default=ZERO)
    result_values = [Decimal(item["results"]) for item in trend if item["results"] is not None]
    max_results = max(result_values, default=ZERO)
    for item in trend:
        item["chart_spend_percent"] = int((Decimal(item["spend"] or "0") / max_spend) * 100) if max_spend else 0
        item["chart_results_percent"] = (
            int((Decimal(item["results"]) / max_results) * 100) if item["results"] is not None and max_results else None
        )
    campaign_rows = list(InsightDaily.objects.filter(account=account, level=InsightLevel.CAMPAIGN, date__range=(date_start, date_end)))
    adset_rows = list(InsightDaily.objects.filter(account=account, level=InsightLevel.ADSET, date__range=(date_start, date_end)))
    ad_rows = list(InsightDaily.objects.filter(account=account, level=InsightLevel.AD, date__range=(date_start, date_end)))
    campaigns = _group_objects(campaign_rows)
    adsets = _group_objects(adset_rows)
    ads = _group_objects(ad_rows)
    anomaly_rows = list(
        Anomaly.objects.filter(account=account, date__range=(date_start, date_end)).exclude(status=AnomalyStatus.RESOLVED)
    )
    anomalies = [
        {
            "id": item.pk,
            "date": item.date.isoformat(),
            "severity": item.severity,
            "rule_id": item.rule_id,
            "name": item.object_name,
            "title": item.title,
            "description": item.description,
            "recommendation": item.recommendation,
            "metrics": item.metrics,
            "status": item.status,
        }
        for item in anomaly_rows
    ]
    sync_rows = SyncRun.objects.filter(account=account, requested_end__gte=date_start, requested_start__lte=date_end).order_by("-created_at")[:20]
    syncs = [
        {
            "id": item.pk,
            "created_at": item.created_at.isoformat(),
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            "status": item.status,
            "start": item.requested_start.isoformat(),
            "end": item.requested_end.isoformat(),
            "records": item.records_count,
            "raw_pages": item.raw_pages_count,
            "attempt": item.attempt,
            "message": item.message,
        }
        for item in sync_rows
    ]
    data_quality = []
    if len(account_rows) != (date_end - date_start).days + 1:
        data_quality.append("Une ou plusieurs journées ne disposent pas d’un agrégat de compte complet.")
    if any(not item.result_verified for item in account_rows + campaign_rows):
        data_quality.append("Au moins une mesure de résultat n’a pas encore été validée face aux colonnes Ads Manager.")
    if any(item["status"] == "partial" for item in syncs):
        data_quality.append("Une synchronisation partielle couvre la période sélectionnée.")
    if any(item.results is None for item in account_rows):
        data_quality.append("Meta n’a renvoyé aucune mesure de résultat pour au moins une journée; la valeur reste manquante et n’est pas remplacée par zéro.")

    snapshot = {
        "schema_version": "1.0",
        "generated_at": timezone.now().isoformat(),
        "period": {"start": date_start.isoformat(), "end": date_end.isoformat()},
        "account": {
            "id": account.pk,
            "external_id": account.external_id,
            "name": account.name,
            "currency": currency,
            "timezone": account.timezone_name,
        },
        "attribution": {
            "action_report_time": "impression",
            "unified_attribution_setting": True,
            "label": "Paramètres d’attribution unifiés du compte/ensemble Meta",
        },
        "synchronization_status": "partial" if data_quality else "complete",
        "data_quality": data_quality,
        "kpis": kpis,
        "comparisons": comparisons,
        "trend": trend,
        "campaigns": campaigns,
        "adsets": adsets[:20],
        "ads": ads[:20],
        "rankings": {
            "campaigns": _cost_ranking(campaigns),
            "ads": _cost_ranking(ads),
        },
        "anomalies": anomalies,
        "synchronizations": syncs,
        "sources": {
            "connector": "Meta Marketing API",
            "api_version": account.connection.api_version,
            "connection_id": account.connection_id,
            "insight_ids": [row.pk for row in account_rows + campaign_rows + adset_rows + ad_rows],
            "raw_payload_ids": sorted({row.raw_payload_id for row in account_rows + campaign_rows + adset_rows + ad_rows if row.raw_payload_id}),
        },
        "definitions": {
            "Meta résultat": "Une seule action Meta configurée par portée; les types d’action qui se chevauchent ne sont jamais additionnés.",
            "Coût par résultat": "Dépense divisée par le nombre de Meta résultats; non calculable si le résultat est nul ou manquant.",
            "CTR": "Clics divisés par impressions, multipliés par 100.",
            "Portée cumulée": "Somme des portées quotidiennes; une personne peut être comptée plusieurs jours sur une période multi-jours.",
            "Classement efficacité": "Coût par résultat le plus faible ou le plus élevé parmi les objets ayant une dépense et une mesure de résultat calculable; ce classement ne prouve aucune cause.",
        },
    }
    snapshot["executive_summary"] = _executive_summary(kpis, comparisons, anomalies, currency, data_quality)
    return snapshot


def build_portfolio_snapshot(accounts, date_start: date, date_end: date):
    accounts = list(accounts)
    if not accounts:
        raise ValueError("Au moins un compte Meta synchronisé est requis.")
    account_ids = [account.pk for account in accounts]
    currencies = {account.currency for account in accounts if account.currency}
    timezones = {account.timezone_name for account in accounts if account.timezone_name}
    mixed_currency = len(currencies) > 1
    currency = next(iter(currencies)) if len(currencies) == 1 else "Devises multiples"
    timezone_name = next(iter(timezones)) if len(timezones) == 1 else "Plusieurs fuseaux horaires"
    all_account_rows = list(
        InsightDaily.objects.select_related("account").filter(
            account_id__in=account_ids,
            level=InsightLevel.ACCOUNT,
            date__range=(date_start, date_end),
        ).order_by("date", "account_id")
    )
    campaign_rows = list(
        InsightDaily.objects.select_related("account").filter(
            account_id__in=account_ids,
            level=InsightLevel.CAMPAIGN,
            date__range=(date_start, date_end),
        )
    )
    adset_rows = list(
        InsightDaily.objects.select_related("account").filter(
            account_id__in=account_ids,
            level=InsightLevel.ADSET,
            date__range=(date_start, date_end),
        )
    )
    ad_rows = list(
        InsightDaily.objects.select_related("account").filter(
            account_id__in=account_ids,
            level=InsightLevel.AD,
            date__range=(date_start, date_end),
        )
    )
    aggregate = aggregate_rows(all_account_rows)
    known_results = _known_result_total(campaign_rows)
    if known_results is None:
        known_results = aggregate["results"]
    portfolio_spend = None if mixed_currency else aggregate["spend"]
    portfolio_cpr = None if mixed_currency or known_results is None else safe_divide(aggregate["spend"], known_results)
    kpis = {
        "has_data": bool(all_account_rows),
        "spend": _decimal(portfolio_spend),
        "results": _decimal(known_results),
        "cost_per_result": _decimal(portfolio_cpr),
        "impressions": aggregate["impressions"],
        "reach_cumulative": aggregate["reach"],
        "clicks": aggregate["clicks"],
        "link_clicks": aggregate["link_clicks"],
        "ctr": _decimal(aggregate["ctr"]),
        "cpc": None if mixed_currency else _decimal(aggregate["cpc"]),
        "cpm": None if mixed_currency else _decimal(aggregate["cpm"]),
    }
    comparisons = {
        str(days): _portfolio_comparison(accounts, date_end, days, mixed_currency=mixed_currency)
        for days in (1, 7, 30)
    }
    trend_start = min(date_start, date_end - timedelta(days=13))
    trend_account_rows = list(
        InsightDaily.objects.select_related("account").filter(
            account_id__in=account_ids,
            level=InsightLevel.ACCOUNT,
            date__range=(trend_start, date_end),
        ).order_by("date", "account_id")
    )
    trend_campaign_rows = list(
        InsightDaily.objects.select_related("account").filter(
            account_id__in=account_ids,
            level=InsightLevel.CAMPAIGN,
            date__range=(trend_start, date_end),
        ).order_by("date", "account_id")
    )
    trend = _portfolio_trend(trend_account_rows, trend_campaign_rows, mixed_currency=mixed_currency)
    campaigns = _group_objects(campaign_rows, include_account=True)
    adsets = _group_objects(adset_rows, include_account=True)
    ads = _group_objects(ad_rows, include_account=True)
    anomaly_rows = list(
        Anomaly.objects.select_related("account")
        .filter(account_id__in=account_ids, date__range=(date_start, date_end))
        .exclude(status=AnomalyStatus.RESOLVED)
    )
    anomalies = [
        {
            "id": item.pk,
            "account_id": item.account_id,
            "account_name": item.account.name,
            "date": item.date.isoformat(),
            "severity": item.severity,
            "rule_id": item.rule_id,
            "name": item.object_name,
            "title": item.title,
            "description": item.description,
            "recommendation": item.recommendation,
            "metrics": item.metrics,
            "status": item.status,
        }
        for item in anomaly_rows
    ]
    sync_rows = list(
        SyncRun.objects.select_related("account")
        .filter(account_id__in=account_ids, requested_end__gte=date_start, requested_start__lte=date_end)
        .order_by("-created_at")[:100]
    )
    syncs = [
        {
            "id": item.pk,
            "account_id": item.account_id,
            "account_name": item.account.name if item.account else "Compte en préparation",
            "created_at": item.created_at.isoformat(),
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            "status": item.status,
            "start": item.requested_start.isoformat(),
            "end": item.requested_end.isoformat(),
            "records": item.records_count,
            "raw_pages": item.raw_pages_count,
            "attempt": item.attempt,
            "message": item.message,
        }
        for item in sync_rows
    ]
    data_quality = []
    expected_days = (date_end - date_start).days + 1
    row_counts = defaultdict(int)
    for row in all_account_rows:
        row_counts[row.account_id] += 1
    incomplete_accounts = [account.name for account in accounts if row_counts[account.pk] != expected_days]
    if incomplete_accounts:
        data_quality.append(
            "Une ou plusieurs journées sont manquantes pour : " + ", ".join(incomplete_accounts) + "."
        )
    if mixed_currency:
        data_quality.append("Les comptes utilisent plusieurs devises; les dépenses et coûts ne sont pas additionnés dans les totaux consolidés.")
    if len(timezones) > 1:
        data_quality.append("Les comptes utilisent plusieurs fuseaux horaires; chaque journée conserve la date fournie par son compte Meta.")
    if any(not item.result_verified for item in all_account_rows + campaign_rows):
        data_quality.append("Au moins une mesure de résultat n’a pas encore été validée face aux colonnes Ads Manager.")
    if any(item["status"] == "partial" for item in syncs):
        data_quality.append("Une synchronisation partielle couvre la période sélectionnée.")
    if any(item.results is None for item in all_account_rows):
        data_quality.append("Meta n’a renvoyé aucune mesure de résultat pour au moins un compte ou une journée; la valeur reste manquante.")

    all_insight_rows = all_account_rows + campaign_rows + adset_rows + ad_rows
    rankings = {
        "campaigns": _cost_ranking(campaigns) if not mixed_currency else {"strongest": None, "weakest": None, "basis": "mixed_currency"},
        "ads": _cost_ranking(ads) if not mixed_currency else {"strongest": None, "weakest": None, "basis": "mixed_currency"},
    }
    snapshot = {
        "schema_version": "1.1",
        "generated_at": timezone.now().isoformat(),
        "period": {"start": date_start.isoformat(), "end": date_end.isoformat()},
        "account": {
            "id": None,
            "external_id": "portfolio",
            "name": "Tous les comptes Meta",
            "currency": currency,
            "timezone": timezone_name,
            "combined": True,
            "count": len(accounts),
            "accounts": [
                {
                    "id": account.pk,
                    "external_id": account.external_id,
                    "name": account.name,
                    "currency": account.currency,
                    "timezone": account.timezone_name,
                }
                for account in accounts
            ],
        },
        "attribution": {
            "action_report_time": "impression",
            "unified_attribution_setting": True,
            "label": "Paramètres d’attribution unifiés propres à chaque compte/ensemble Meta",
        },
        "synchronization_status": "partial" if data_quality else "complete",
        "data_quality": data_quality,
        "kpis": kpis,
        "comparisons": comparisons,
        "trend": trend,
        "campaigns": campaigns,
        "adsets": adsets[:100],
        "ads": ads[:100],
        "rankings": rankings,
        "anomalies": anomalies,
        "synchronizations": syncs,
        "sources": {
            "connector": "Meta Marketing API",
            "api_version": ", ".join(sorted({account.connection.api_version for account in accounts})),
            "connection_ids": [account.connection_id for account in accounts],
            "insight_ids": [row.pk for row in all_insight_rows],
            "raw_payload_ids": sorted({row.raw_payload_id for row in all_insight_rows if row.raw_payload_id}),
        },
        "definitions": {
            "Meta résultat": "Somme des mesures de résultat Meta normalisées pour les comptes inclus; les types d’action qui se chevauchent ne sont jamais additionnés.",
            "Coût par résultat": "Dépense divisée par le nombre de Meta résultats; non calculable si le résultat est nul, manquant ou si plusieurs devises sont présentes.",
            "CTR": "Clics divisés par impressions, multipliés par 100.",
            "Portée cumulée": "Somme des portées quotidiennes et des comptes; une personne peut être comptée plusieurs fois.",
            "Rapport consolidé": "Chaque ligne conserve le compte Meta source, les Insight IDs et les Payload IDs qui permettent sa traçabilité.",
        },
    }
    snapshot["executive_summary"] = _executive_summary(
        kpis,
        comparisons,
        anomalies,
        currency,
        data_quality,
        mixed_currency=mixed_currency,
    )
    return snapshot


def _generate_report_version(report_run, accounts, snapshot_builder, date_start, date_end, *, source, user, base_prefix):
    accounts = list(accounts)
    report_run.status = ReportStatus.GENERATING
    report_run.source = source
    if user:
        report_run.created_by = user
    report_run.error_message = ""
    report_run.save()
    report_run.included_accounts.set(accounts)
    try:
        with transaction.atomic():
            snapshot = snapshot_builder()
            canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            snapshot_hash = hashlib.sha256(canonical).hexdigest()
            next_version = (report_run.versions.aggregate(max_version=Max("version"))["max_version"] or 0) + 1
            version = ReportVersion.objects.create(
                report_run=report_run,
                version=next_version,
                snapshot=snapshot,
                snapshot_hash=snapshot_hash,
            )
            base_name = f"{base_prefix}-{date_start.isoformat()}-{date_end.isoformat()}-v{next_version}"
            version.excel_file.save(f"{base_name}.xlsx", ContentFile(build_excel(snapshot)), save=False)
            version.pdf_file.save(f"{base_name}.pdf", ContentFile(build_pdf(snapshot)), save=False)
            version.save()
        report_run.status = ReportStatus.PARTIAL if snapshot["data_quality"] else ReportStatus.READY
        report_run.save(update_fields=["status", "updated_at"])
        Anomaly.objects.filter(
            account__in=accounts,
            rule_id="REPORT_GENERATION_FAILED",
            status__in=[AnomalyStatus.OPEN, AnomalyStatus.ACKNOWLEDGED],
        ).update(status=AnomalyStatus.RESOLVED, resolved_at=timezone.now())
        return version
    except Exception as exc:
        report_run.status = ReportStatus.FAILED
        report_run.error_message = f"{type(exc).__name__}: la génération du rapport a échoué."
        report_run.save(update_fields=["status", "error_message", "updated_at"])
        for account in accounts:
            Anomaly.objects.update_or_create(
                account=account,
                date=date_end,
                level=InsightLevel.ACCOUNT,
                object_external_id=account.external_id,
                rule_id="REPORT_GENERATION_FAILED",
                defaults={
                    "object_name": account.name,
                    "severity": Severity.CRITICAL,
                    "title": "Génération du rapport impossible",
                    "description": "Le rapport demandé n’a pas pu être produit; aucune version incomplète n’est présentée comme prête.",
                    "recommendation": "Contrôler l’état des données, l’espace disque et les journaux du worker avant de relancer la génération.",
                    "metrics": {"report_run_id": report_run.pk, "error_type": type(exc).__name__},
                    "status": AnomalyStatus.OPEN,
                    "resolved_at": None,
                },
            )
        raise


def generate_report(account, date_start: date, date_end: date, *, source="manual", user=None):
    report_run, _ = ReportRun.objects.get_or_create(
        account=account,
        scope=ReportScope.ACCOUNT,
        date_start=date_start,
        date_end=date_end,
        defaults={"source": source, "created_by": user, "status": ReportStatus.PENDING},
    )
    return _generate_report_version(
        report_run,
        [account],
        lambda: build_snapshot(account, date_start, date_end),
        date_start,
        date_end,
        source=source,
        user=user,
        base_prefix="rapport-meta",
    )


def generate_portfolio_report(accounts, date_start: date, date_end: date, *, source="manual", user=None):
    accounts = sorted({account.pk: account for account in accounts}.values(), key=lambda account: account.pk)
    if not accounts:
        raise ValueError("Au moins un compte Meta synchronisé est requis.")
    report_run, _ = ReportRun.objects.get_or_create(
        account=None,
        scope=ReportScope.PORTFOLIO,
        date_start=date_start,
        date_end=date_end,
        defaults={"source": source, "created_by": user, "status": ReportStatus.PENDING},
    )
    return _generate_report_version(
        report_run,
        accounts,
        lambda: build_portfolio_snapshot(accounts, date_start, date_end),
        date_start,
        date_end,
        source=source,
        user=user,
        base_prefix="rapport-meta-consolide",
    )
