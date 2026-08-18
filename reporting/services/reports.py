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
    }


def _group_objects(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row.object_external_id, row.object_name, row.result_action_type, row.result_label, row.result_verified)].append(row)
    output = []
    for (object_id, name, action_type, label, verified), members in grouped.items():
        metrics = aggregate_rows(members)
        output.append(
            {
                "object_id": object_id,
                "name": name,
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


def _executive_summary(kpis, comparisons, anomalies, currency, data_quality):
    if not kpis["has_data"]:
        return "Aucune donnée Meta complète n’est disponible pour la période sélectionnée. Vérifiez la synchronisation avant toute interprétation."
    spend = Decimal(kpis["spend"] or "0")
    cpl = kpis["cost_per_result"]
    if kpis["results"] is None:
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
        InsightDaily.objects.filter(account=account, level=InsightLevel.ACCOUNT, date__range=(date_start, date_end)).order_by("date")
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
    ).order_by("date")
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


def generate_report(account, date_start: date, date_end: date, *, source="manual", user=None):
    report_run, _ = ReportRun.objects.get_or_create(
        account=account,
        date_start=date_start,
        date_end=date_end,
        defaults={"source": source, "created_by": user, "status": ReportStatus.PENDING},
    )
    report_run.status = ReportStatus.GENERATING
    report_run.source = source
    if user:
        report_run.created_by = user
    report_run.error_message = ""
    report_run.save()
    try:
        with transaction.atomic():
            snapshot = build_snapshot(account, date_start, date_end)
            canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            snapshot_hash = hashlib.sha256(canonical).hexdigest()
            next_version = (report_run.versions.aggregate(max_version=Max("version"))["max_version"] or 0) + 1
            version = ReportVersion.objects.create(
                report_run=report_run,
                version=next_version,
                snapshot=snapshot,
                snapshot_hash=snapshot_hash,
            )
            base_name = f"rapport-meta-{date_start.isoformat()}-{date_end.isoformat()}-v{next_version}"
            version.excel_file.save(f"{base_name}.xlsx", ContentFile(build_excel(snapshot)), save=False)
            version.pdf_file.save(f"{base_name}.pdf", ContentFile(build_pdf(snapshot)), save=False)
            version.save()
        report_run.status = ReportStatus.PARTIAL if snapshot["data_quality"] else ReportStatus.READY
        report_run.save(update_fields=["status", "updated_at"])
        Anomaly.objects.filter(
            account=account,
            rule_id="REPORT_GENERATION_FAILED",
            status__in=[AnomalyStatus.OPEN, AnomalyStatus.ACKNOWLEDGED],
        ).update(status=AnomalyStatus.RESOLVED, resolved_at=timezone.now())
        return version
    except Exception as exc:
        report_run.status = ReportStatus.FAILED
        report_run.error_message = f"{type(exc).__name__}: la génération du rapport a échoué."
        report_run.save(update_fields=["status", "error_message", "updated_at"])
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
