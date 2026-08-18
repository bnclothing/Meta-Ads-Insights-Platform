from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from reporting.models import Anomaly, AnomalyStatus, AppSettings, CampaignSnapshot, InsightDaily, InsightLevel, Severity

from .metrics import ZERO, average, percent_change, safe_divide


RULE_IDS = {
    "INSUFFICIENT_HISTORY",
    "ZERO_RESULTS_HIGH_SPEND",
    "CPL_INCREASE",
    "RESULTS_DROP",
    "SPEND_CHANGE",
    "CTR_CHANGE",
    "HIGH_FREQUENCY",
    "ACTIVE_NO_DELIVERY",
    "METRIC_UNAVAILABLE",
}


def _metric_json(value):
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    return value


def _upsert(account, row, rule_id, severity, title, description, recommendation, metrics):
    anomaly, _ = Anomaly.objects.update_or_create(
        account=account,
        date=row.date,
        level=row.level,
        object_external_id=row.object_external_id,
        rule_id=rule_id,
        defaults={
            "object_name": row.object_name,
            "severity": severity,
            "title": title,
            "description": description,
            "recommendation": recommendation,
            "metrics": {key: _metric_json(value) for key, value in metrics.items()},
            "status": AnomalyStatus.OPEN,
            "resolved_at": None,
        },
    )
    return anomaly


@transaction.atomic
def evaluate_alerts(account, report_date):
    settings_obj = AppSettings.load()
    current_rows = list(
        InsightDaily.objects.filter(account=account, date=report_date, level__in=[InsightLevel.ACCOUNT, InsightLevel.CAMPAIGN])
    )
    generated: set[tuple[str, str, str]] = set()

    for row in current_rows:
        history = list(
            InsightDaily.objects.filter(
                account=account,
                level=row.level,
                object_external_id=row.object_external_id,
                date__gte=report_date - timedelta(days=7),
                date__lt=report_date,
                spend__gt=0,
            ).order_by("date")
        )
        if len(history) < 3:
            _upsert(
                account,
                row,
                "INSUFFICIENT_HISTORY",
                Severity.INFO,
                "Historique insuffisant",
                f"Seulement {len(history)} jour(s) antérieur(s) avec dépense sont disponibles.",
                "Attendre au moins trois jours comparables avant d’interpréter une variation relative.",
                {"available_days": len(history), "required_days": 3},
            )
            generated.add((row.level, row.object_external_id, "INSUFFICIENT_HISTORY"))
            if row.results is None:
                _upsert(
                    account,
                    row,
                    "METRIC_UNAVAILABLE",
                    Severity.INFO,
                    "Mesure de résultat indisponible",
                    "Meta n’a renvoyé aucune mesure de résultat pour cette portée; la valeur reste manquante.",
                    "Contrôler la colonne Résultats, le type d’action et l’attribution dans Ads Manager avant toute interprétation.",
                    {"spend": row.spend},
                )
                generated.add((row.level, row.object_external_id, "METRIC_UNAVAILABLE"))
            continue

        avg_spend = average(item.spend for item in history)
        results_history_complete = all(item.results is not None for item in history)
        avg_results = average(item.results for item in history) if results_history_complete else None
        avg_ctr = average((item.ctr or ZERO) for item in history)
        historical_spend = sum((item.spend for item in history), ZERO)
        historical_results = sum((item.results for item in history), ZERO) if results_history_complete else None
        avg_cpl = safe_divide(historical_spend, historical_results) if historical_results is not None else None
        current_results = row.results
        current_cpl = safe_divide(row.spend, current_results) if current_results is not None else None

        rules = []
        if current_results is None:
            rules.append((
                "METRIC_UNAVAILABLE", Severity.INFO, "Mesure de résultat indisponible",
                "Meta n’a renvoyé aucune mesure de résultat pour cette portée; la valeur reste manquante.",
                "Contrôler la colonne Résultats, le type d’action et l’attribution dans Ads Manager avant toute interprétation.",
                {"spend": row.spend},
            ))
        if current_results == 0 and avg_cpl and row.spend >= avg_cpl * settings_obj.zero_result_spend_cpl_multiplier:
            rules.append((
                "ZERO_RESULTS_HIGH_SPEND", Severity.HIGH, "Dépense sans résultat",
                "La dépense dépasse le seuil calculé sans aucun résultat Meta enregistré.",
                "Vérifier la diffusion, le formulaire ou la destination, puis comparer les colonnes Résultats et Attribution dans Ads Manager.",
                {"spend": row.spend, "threshold": avg_cpl * settings_obj.zero_result_spend_cpl_multiplier, "results": current_results},
            ))
        if current_cpl and avg_cpl:
            change = percent_change(current_cpl, avg_cpl)
            if change is not None and change >= settings_obj.cpl_increase_percent:
                rules.append((
                    "CPL_INCREASE", Severity.HIGH, "Coût par résultat en hausse",
                    f"Le coût par résultat augmente de {change.quantize(Decimal('0.1'))} % par rapport aux sept jours précédents.",
                    "Contrôler les changements récents de ciblage, créatif, budget et placement sans attribuer automatiquement la cause.",
                    {"current_cpl": current_cpl, "baseline_cpl": avg_cpl, "change_percent": change},
                ))
        result_change = percent_change(current_results, avg_results) if current_results is not None and avg_results is not None else None
        spend_change = percent_change(row.spend, avg_spend)
        if result_change is not None and result_change <= -settings_obj.result_drop_percent and row.spend >= avg_spend * Decimal("0.80"):
            rules.append((
                "RESULTS_DROP", Severity.HIGH, "Résultats en baisse",
                f"Les résultats reculent de {abs(result_change).quantize(Decimal('0.1'))} % avec une dépense stable ou supérieure.",
                "Comparer la diffusion, la fréquence, le CTR et les changements de campagne avant de décider d’une action.",
                {"current_results": current_results, "baseline_results": avg_results, "change_percent": result_change},
            ))
        if spend_change is not None and abs(spend_change) >= settings_obj.spend_change_percent:
            rules.append((
                "SPEND_CHANGE", Severity.MEDIUM, "Variation importante de dépense",
                f"La dépense varie de {spend_change.quantize(Decimal('0.1'))} % par rapport à la moyenne récente.",
                "Vérifier les budgets, la planification et l’état de diffusion dans Ads Manager.",
                {"current_spend": row.spend, "baseline_spend": avg_spend, "change_percent": spend_change},
            ))
        if row.ctr is not None and avg_ctr:
            ctr_change = percent_change(row.ctr, avg_ctr)
            if ctr_change is not None and abs(ctr_change) >= settings_obj.ctr_change_percent:
                rules.append((
                    "CTR_CHANGE", Severity.MEDIUM, "Variation importante du CTR",
                    f"Le CTR varie de {ctr_change.quantize(Decimal('0.1'))} % par rapport aux sept jours précédents.",
                    "Contrôler les créatifs, les audiences et les placements associés à cette variation.",
                    {"current_ctr": row.ctr, "baseline_ctr": avg_ctr, "change_percent": ctr_change},
                ))
        frequency_rows = history + [row]
        frequency_7d = safe_divide(
            Decimal(sum(item.impressions for item in frequency_rows)),
            Decimal(sum(item.reach for item in frequency_rows)),
        )
        if frequency_7d is not None and frequency_7d > settings_obj.frequency_threshold:
            rules.append((
                "HIGH_FREQUENCY", Severity.MEDIUM, "Fréquence élevée",
                f"La fréquence calculée sur les mesures quotidiennes des sept derniers jours atteint {frequency_7d.quantize(Decimal('0.01'))}.",
                "Examiner la taille d’audience et la fatigue créative avant toute modification.",
                {"frequency_7d": frequency_7d, "threshold": settings_obj.frequency_threshold},
            ))
        campaign_is_active = (
            row.level == InsightLevel.CAMPAIGN
            and CampaignSnapshot.objects.filter(
                account=account,
                external_id=row.object_external_id,
                effective_status__iexact="ACTIVE",
            ).exists()
        )
        if campaign_is_active and row.spend == 0 and row.impressions == 0:
            rules.append((
                "ACTIVE_NO_DELIVERY", Severity.INFO, "Aucune diffusion observée",
                "Aucune impression ni dépense n’est présente pour cette campagne sur la journée.",
                "Vérifier le statut effectif, le calendrier et l’éligibilité de la campagne.",
                {"spend": row.spend, "impressions": row.impressions},
            ))

        for rule in rules:
            _upsert(account, row, *rule)
            generated.add((row.level, row.object_external_id, rule[0]))

    open_existing = Anomaly.objects.filter(account=account, date=report_date, rule_id__in=RULE_IDS, status=AnomalyStatus.OPEN)
    for anomaly in open_existing:
        if (anomaly.level, anomaly.object_external_id, anomaly.rule_id) not in generated:
            anomaly.status = AnomalyStatus.RESOLVED
            anomaly.resolved_at = timezone.now()
            anomaly.save(update_fields=["status", "resolved_at", "updated_at"])
    return list(Anomaly.objects.filter(account=account, date=report_date, status=AnomalyStatus.OPEN))
