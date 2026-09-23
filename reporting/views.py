from __future__ import annotations

import json
from collections import Counter
from datetime import date, time, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from reporting.connectors import DataSheetError, MetaMarketingConnector, load_lead_dataset, source_choices
from reporting.forms import AppSettingsForm, MetaConnectionForm, MetricMappingForm
from reporting.models import (
    ActionMetricDaily,
    AdAccount,
    Anomaly,
    AnomalyStatus,
    AppSettings,
    ConnectionStatus,
    InsightDaily,
    InsightLevel,
    MetaConnection,
    MetricMapping,
    ReportRun,
    ReportScope,
    ReportVersion,
    SyncRun,
    SyncStatus,
)
from reporting.services.audit import record_audit
from reporting.services.metrics import ZERO, aggregate_rows, percent_change, safe_divide
from reporting.services.reports import generate_portfolio_report, generate_report
from reporting.tasks import synchronize_meta


def _parse_date(value, fallback):
    try:
        return date.fromisoformat(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _selected_connection(request, payload=None):
    requested_id = None
    if isinstance(payload, dict):
        requested_id = payload.get("connection_id")
    requested_id = requested_id or request.GET.get("connection") or request.POST.get("connection_id")
    if requested_id:
        try:
            connection_obj = MetaConnection.objects.get(pk=int(requested_id))
        except (MetaConnection.DoesNotExist, TypeError, ValueError):
            connection_obj = None
        if connection_obj:
            request.session["selected_meta_connection_id"] = connection_obj.pk
            return connection_obj

    stored_id = request.session.get("selected_meta_connection_id")
    if stored_id:
        connection_obj = MetaConnection.objects.filter(pk=stored_id).first()
        if connection_obj:
            return connection_obj
        request.session.pop("selected_meta_connection_id", None)

    connection_obj = MetaConnection.objects.filter(is_active=True).order_by("id").first() or MetaConnection.objects.order_by("id").first()
    if connection_obj:
        request.session["selected_meta_connection_id"] = connection_obj.pk
    return connection_obj


REPORTING_ALL_SESSION_KEY = "reporting_all_meta_accounts"


def _selected_reporting_scope(request):
    requested = request.GET.get("connection") or request.POST.get("connection_id")
    if requested == "all":
        request.session[REPORTING_ALL_SESSION_KEY] = True
        return None, True
    if requested:
        request.session[REPORTING_ALL_SESSION_KEY] = False
        return _selected_connection(request), False
    if request.session.get(REPORTING_ALL_SESSION_KEY, True):
        request.session[REPORTING_ALL_SESSION_KEY] = True
        return None, True
    return _selected_connection(request), False


def _active_account(connection_obj):
    if not connection_obj:
        return None
    configured_id = connection_obj.ad_account_external_id.removeprefix("act_")
    accounts = AdAccount.objects.select_related("connection").filter(connection=connection_obj)
    return accounts.filter(external_id=configured_id).first() or accounts.order_by("-id").first()


def _navigation_context(connection_obj, *, allow_all=False, all_selected=False):
    return {
        "meta_connections": MetaConnection.objects.order_by("name", "id"),
        "selected_connection": connection_obj,
        "allow_all_connections": allow_all,
        "all_connections_selected": all_selected,
    }


def _reporting_connections(connection_obj, all_selected):
    if all_selected:
        return list(MetaConnection.objects.filter(is_active=True).order_by("name", "id"))
    return [connection_obj] if connection_obj else []


def _reporting_accounts(connections):
    return [account for connection_obj in connections if (account := _active_account(connection_obj))]


SYNC_LEVELS = list(InsightLevel.values)
FINISHED_SYNC_STATUSES = (SyncStatus.SUCCESS, SyncStatus.PARTIAL)
ACTIVE_SYNC_STATUSES = (SyncStatus.PENDING, SyncStatus.RUNNING)


def _covering_sync(connection_obj, range_start, range_end, statuses, levels=None):
    """Return a run that fully checked this period at all requested levels."""
    if not connection_obj:
        return None
    required_levels = set(levels or SYNC_LEVELS)
    candidates = SyncRun.objects.filter(
        connection=connection_obj,
        status__in=statuses,
        requested_start__lte=range_start,
        requested_end__gte=range_end,
    ).order_by("-created_at")
    return next((run for run in candidates if required_levels.issubset(set(run.levels or []))), None)


def _backfill_sync(connection_obj, days, statuses):
    """Return a full-level run long enough to count as the initial history load."""
    if not connection_obj:
        return None
    required_span = max(int(days) - 1, 0)
    candidates = SyncRun.objects.filter(connection=connection_obj, status__in=statuses).order_by("-created_at")
    return next(
        (
            run
            for run in candidates
            if (run.requested_end - run.requested_start).days >= required_span
            and set(SYNC_LEVELS).issubset(set(run.levels or []))
        ),
        None,
    )


def _queue_sync(connection_obj, range_start, range_end, *, trigger, levels=None, generate_report=False):
    """Queue one sync, reusing an already-running job that covers the same range."""
    requested_levels = list(levels or SYNC_LEVELS)
    existing = _covering_sync(connection_obj, range_start, range_end, ACTIVE_SYNC_STATUSES, requested_levels)
    if existing:
        return existing, False

    run = SyncRun.objects.create(
        connection=connection_obj,
        requested_start=range_start,
        requested_end=range_end,
        trigger=trigger,
        levels=requested_levels,
    )
    task = synchronize_meta.delay(run.pk, generate_report=generate_report)
    run.task_id = task.id or ""
    run.save(update_fields=["task_id", "updated_at"])
    return run, True


def _selected_date(account, request):
    latest = (
        InsightDaily.objects.filter(account=account, level=InsightLevel.ACCOUNT).order_by("-date").values_list("date", flat=True).first()
        if account
        else None
    )
    return _parse_date(request.GET.get("date"), latest or (timezone.localdate() - timedelta(days=1)))


def _selected_range(account, request):
    fallback_end = _selected_date(account, request)
    legacy_date = request.GET.get("date")
    end = _parse_date(request.GET.get("end") or legacy_date, fallback_end)
    start = _parse_date(request.GET.get("start") or legacy_date, end)
    if start > end:
        start, end = end, start
    if (end - start).days > 365:
        start = end - timedelta(days=365)
    return start, end


def _selected_range_for_accounts(accounts, request):
    latest = (
        InsightDaily.objects.filter(account__in=accounts, level=InsightLevel.ACCOUNT)
        .order_by("-date")
        .values_list("date", flat=True)
        .first()
        if accounts
        else None
    )
    fallback_end = latest or (timezone.localdate() - timedelta(days=1))
    legacy_date = request.GET.get("date")
    end = _parse_date(request.GET.get("end") or legacy_date, fallback_end)
    start = _parse_date(request.GET.get("start") or legacy_date, end)
    if start > end:
        start, end = end, start
    if (end - start).days > 365:
        start = end - timedelta(days=365)
    return start, end


def _display_decimal(value, places="0.01"):
    if value is None:
        return "—"
    quantized = Decimal(value).quantize(Decimal(places))
    return f"{quantized:,.{abs(quantized.as_tuple().exponent)}f}".replace(",", " ")


def _known_result_total(rows):
    """Sum campaign results Meta actually normalized, without inventing missing values."""
    values = [row.results for row in rows if row.results is not None]
    return sum(values, ZERO) if values else None


def _dashboard_payload(account, range_start, range_end, *, combined=False):
    accounts = list(account) if isinstance(account, (list, tuple)) else ([account] if account else [])
    if not accounts:
        return {
            "account": None,
            "date": range_end.isoformat(),
            "start": range_start.isoformat(),
            "end": range_end.isoformat(),
            "current": False,
            "kpis": [],
            "campaigns": [],
            "alerts": [],
            "trend": [],
            "last_sync": None,
            "data_state": "not_configured",
        }

    currencies = {item.currency for item in accounts if item.currency}
    mixed_currency = len(currencies) > 1
    common_currency = next(iter(currencies)) if len(currencies) == 1 else ""
    current_rows = list(
        InsightDaily.objects.filter(
            account__in=accounts,
            level=InsightLevel.ACCOUNT,
            date__range=(range_start, range_end),
        ).order_by("date", "account_id")
    )
    period_days = (range_end - range_start).days + 1
    previous_end = range_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days - 1)
    previous_rows = list(
        InsightDaily.objects.filter(
            account__in=accounts,
            level=InsightLevel.ACCOUNT,
            date__range=(previous_start, previous_end),
        ).order_by("date", "account_id")
    )
    campaign_rows = list(
        InsightDaily.objects.select_related("account").filter(
            account__in=accounts,
            level=InsightLevel.CAMPAIGN,
            date__range=(range_start, range_end),
        ).order_by("date", "object_external_id")
    )
    previous_campaign_rows = list(
        InsightDaily.objects.filter(
            account__in=accounts,
            level=InsightLevel.CAMPAIGN,
            date__range=(previous_start, previous_end),
        ).order_by("date", "object_external_id")
    )
    current_summary = aggregate_rows(current_rows)
    previous_summary = aggregate_rows(previous_rows)
    current_campaign_results = _known_result_total(campaign_rows)
    previous_campaign_results = _known_result_total(previous_campaign_rows)
    if current_campaign_results is not None:
        current_summary["results"] = current_campaign_results
        current_summary["cost_per_result"] = None if mixed_currency else safe_divide(current_summary["spend"], current_campaign_results)
    if previous_campaign_results is not None:
        previous_summary["results"] = previous_campaign_results
        previous_summary["cost_per_result"] = None if mixed_currency else safe_divide(previous_summary["spend"], previous_campaign_results)

    if current_rows:
        latest_row = current_rows[-1]
        values = [
            (
                "Dépenses",
                None if mixed_currency else current_summary["spend"],
                None if mixed_currency else percent_change(current_summary["spend"], previous_summary["spend"]),
                common_currency,
                "money",
            ),
            (
                latest_row.result_label or "Meta résultats",
                current_summary["results"],
                (
                    percent_change(current_summary["results"], previous_summary["results"])
                    if current_summary["results"] is not None and previous_summary["results"] is not None
                    else None
                ),
                "",
                "number",
            ),
            (
                "Coût / résultat",
                current_summary["cost_per_result"],
                (
                    percent_change(current_summary["cost_per_result"], previous_summary["cost_per_result"])
                    if current_summary["cost_per_result"] is not None and previous_summary["cost_per_result"] is not None
                    else None
                ),
                common_currency,
                "money",
            ),
            (
                "Impressions",
                Decimal(current_summary["impressions"]),
                percent_change(Decimal(current_summary["impressions"]), Decimal(previous_summary["impressions"])),
                "",
                "integer",
            ),
        ]
        kpis = []
        for label, value, delta, suffix, kind in values:
            if value is None:
                display = "Devises multiples" if mixed_currency and kind == "money" else "Manquant"
            elif kind == "integer":
                display = f"{int(value):,}".replace(",", " ")
            else:
                display = _display_decimal(value)
            positive = delta is not None and ((label == "Coût / résultat" and delta < 0) or (label != "Coût / résultat" and delta >= 0))
            kpis.append(
                {
                    "label": label,
                    "value": display,
                    "suffix": suffix,
                    "delta": _display_decimal(delta, "0.1") if delta is not None else None,
                    "tone": "positive" if positive else "warning",
                }
            )
    else:
        kpis = []

    grouped_campaigns = {}
    for row in campaign_rows:
        group = grouped_campaigns.setdefault(
            (row.account_id, row.object_external_id),
            {
                "id": row.object_external_id,
                "name": row.object_name,
                "account_name": row.account.name or row.account.external_id,
                "currency": row.currency,
                "rows": [],
                "verified": True,
                "label": row.result_label,
            },
        )
        group["name"] = row.object_name or group["name"]
        group["currency"] = row.currency
        group["rows"].append(row)
        group["verified"] = group["verified"] and row.result_verified
        group["label"] = row.result_label or group["label"]

    campaigns = []
    for group in grouped_campaigns.values():
        summary = aggregate_rows(group.pop("rows"))
        campaigns.append(
            {
                **group,
                "spend": _display_decimal(summary["spend"]),
                "results": _display_decimal(summary["results"]),
                "cpr": _display_decimal(summary["cost_per_result"]),
                "spend_value": summary["spend"],
                "results_value": summary["results"],
            }
        )
    result_shares_available = bool(campaigns) and all(item["results_value"] is not None for item in campaigns)
    total_results = sum((item["results_value"] for item in campaigns), ZERO) if result_shares_available else None
    for item in campaigns:
        item["share"] = int((item["results_value"] / total_results * 100)) if item["results_value"] is not None and total_results else 0
        item["share_available"] = result_shares_available and bool(total_results)
    campaigns.sort(
        key=lambda item: (
            item["results_value"] is not None,
            item["results_value"] if item["results_value"] is not None else ZERO,
            item["spend_value"],
        ),
        reverse=True,
    )
    campaigns = campaigns[:8]
    for item in campaigns:
        item.pop("spend_value", None)
        item.pop("results_value", None)

    alerts_qs = Anomaly.objects.filter(
        account__in=accounts,
        date__range=(range_start, range_end),
        status__in=[AnomalyStatus.OPEN, AnomalyStatus.ACKNOWLEDGED],
    )
    severity_order = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    alerts = sorted(
        [
            {
                "id": item.pk,
                "severity": item.severity,
                "title": item.title,
                "name": item.object_name,
                "date": item.date,
                "rule_id": item.rule_id,
                "recommendation": item.recommendation,
            }
            for item in alerts_qs
        ],
        key=lambda item: severity_order.get(item["severity"], 9),
    )
    campaign_results_by_date = {}
    for row in campaign_rows:
        if row.results is not None:
            campaign_results_by_date[row.date] = campaign_results_by_date.get(row.date, ZERO) + row.results
    trend_by_date = {}
    for row in current_rows:
        point = trend_by_date.setdefault(row.date, {"spend": ZERO, "account_results": []})
        point["spend"] += row.spend
        if row.results is not None:
            point["account_results"].append(row.results)
    trend = []
    for day, point in sorted(trend_by_date.items()):
        fallback_results = sum(point["account_results"], ZERO) if point["account_results"] and not campaign_rows else None
        trend.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%d/%m"),
                "spend": None if mixed_currency else float(point["spend"]),
                "results": float(campaign_results_by_date[day]) if day in campaign_results_by_date else (float(fallback_results) if fallback_results is not None else None),
            }
        )
    last_sync = (
        SyncRun.objects.filter(account__in=accounts, requested_end__gte=range_start, requested_start__lte=range_end)
        .order_by("-created_at")
        .first()
    )
    sync_finished_without_data = bool(
        not current_rows
        and last_sync
        and last_sync.status == SyncStatus.SUCCESS
        and last_sync.requested_start <= range_start
        and last_sync.requested_end >= range_end
    )
    return {
        "account": {
            "id": None if combined else accounts[0].pk,
            "name": "Tous les comptes Meta" if combined else accounts[0].name,
            "currency": common_currency,
            "timezone": accounts[0].timezone_name if len({item.timezone_name for item in accounts}) == 1 else "Plusieurs fuseaux horaires",
            "count": len(accounts),
        },
        "date": range_end.isoformat(),
        "start": range_start.isoformat(),
        "end": range_end.isoformat(),
        "current": bool(current_rows),
        "kpis": kpis,
        "campaigns": campaigns,
        "alerts": alerts,
        "trend": trend,
        "last_sync": last_sync,
        "mixed_currency": mixed_currency,
        "data_state": (
            "empty"
            if sync_finished_without_data
            else ("complete" if current_rows and (not last_sync or last_sync.status == "success") else "missing")
        ),
    }


@login_required
def dashboard(request):
    connection_obj, all_selected = _selected_reporting_scope(request)
    connections = _reporting_connections(connection_obj, all_selected)
    accounts = _reporting_accounts(connections)
    app_settings = AppSettings.load()
    range_start, range_end = _selected_range_for_accounts(accounts, request)
    context = _dashboard_payload(accounts if all_selected else (accounts[0] if accounts else None), range_start, range_end, combined=all_selected)
    automatic_connections = list(
        MetaConnection.objects.filter(is_active=True, status=ConnectionStatus.CONNECTED).order_by("name", "id")
    )
    explicit_range = any(request.GET.get(key) for key in ("start", "end", "date"))
    if explicit_range:
        refresh_start, refresh_end = range_start, range_end
    else:
        refresh_end = timezone.localdate()
        refresh_start = refresh_end - timedelta(days=max(app_settings.rolling_resync_days, 1) - 1)
    automatic_reload_completed = request.GET.get("meta_fresh") == "1"
    automatic_sync_jobs = []
    sync_targets = [
        {"connection_id": item.pk, "start": range_start.isoformat(), "end": range_end.isoformat()}
        for item in automatic_connections
    ]
    for item in automatic_connections:
        account = _active_account(item)
        active_backfill = None if account else _backfill_sync(item, app_settings.history_backfill_days, ACTIVE_SYNC_STATUSES)
        automatic_start = (
            refresh_start
            if account
            else (active_backfill.requested_start if active_backfill else refresh_end - timedelta(days=app_settings.history_backfill_days - 1))
        )
        automatic_end = active_backfill.requested_end if active_backfill else refresh_end
        if not automatic_reload_completed:
            automatic_sync_jobs.append(
                {
                    "connection_id": item.pk,
                    "start": automatic_start,
                    "end": automatic_end,
                }
            )
    automatic_sync_enabled = bool(automatic_sync_jobs)
    loading_start = min((job["start"] for job in automatic_sync_jobs), default=range_start)
    loading_end = max((job["end"] for job in automatic_sync_jobs), default=range_end)
    context.update(
        {
            "active_page": "dashboard",
            "selected_start": range_start,
            "selected_end": range_end,
            "connection_ready": bool(connections) and all(item.status == ConnectionStatus.CONNECTED for item in connections),
            "backfill_days": app_settings.history_backfill_days,
            "backfill_start": range_end - timedelta(days=app_settings.history_backfill_days - 1),
            "automatic_sync_enabled": automatic_sync_enabled,
            "automatic_sync_jobs": automatic_sync_jobs,
            "automatic_sync_start": loading_start,
            "automatic_sync_end": loading_end,
            "sync_targets": sync_targets,
            "combined_accounts": all_selected,
            **_navigation_context(connection_obj, allow_all=True, all_selected=all_selected),
        }
    )
    return render(request, "reporting/dashboard.html", context)


@login_required
def data_sheet(request):
    choices = source_choices()
    choice_labels = dict(choices)
    source = request.GET.get("source", "all")
    if source not in choice_labels:
        source = "all"

    dataset = None
    connection_error = ""
    try:
        dataset = load_lead_dataset(source, refresh=request.GET.get("refresh") == "1")
        all_leads = list(dataset.leads)
    except DataSheetError as exc:
        all_leads = []
        connection_error = str(exc)

    latest_available = max((lead.entered_on for lead in all_leads), default=None)
    fallback_end = latest_available or timezone.localdate()
    range_end = _parse_date(request.GET.get("end"), fallback_end)
    range_start = _parse_date(request.GET.get("start"), range_end - timedelta(days=29))
    if range_start > range_end:
        range_start, range_end = range_end, range_start
    if (range_end - range_start).days > 1095:
        range_start = range_end - timedelta(days=1095)

    query = request.GET.get("q", "").strip()
    query_key = query.casefold()
    selected_leads = [
        lead
        for lead in all_leads
        if range_start <= lead.entered_on <= range_end and (not query_key or query_key in lead.search_text)
    ]
    selected_leads.sort(key=lambda lead: (lead.entered_on, lead.row_number), reverse=True)

    daily_counts = Counter(lead.entered_on for lead in selected_leads)
    trend = [
        {"date": day.isoformat(), "label": day.strftime("%d/%m"), "leads": daily_counts.get(day, 0)}
        for day in (range_start + timedelta(days=offset) for offset in range((range_end - range_start).days + 1))
    ]
    status_counts = Counter(lead.status or "Sans statut" for lead in selected_leads)
    top_statuses = [
        {
            "label": label,
            "count": count,
            "share": round((count / len(selected_leads)) * 100) if selected_leads else 0,
        }
        for label, count in status_counts.most_common(5)
    ]
    source_counts = Counter(lead.source for lead in selected_leads)
    source_breakdown = [
        {
            "key": source_key,
            "label": label,
            "count": source_counts.get(source_key, 0),
            "share": round((source_counts.get(source_key, 0) / len(selected_leads)) * 100) if selected_leads else 0,
        }
        for source_key, label in choices
        if source_key != "all"
    ]
    unique_contacts = {
        "".join(character for character in lead.contact.casefold() if character.isalnum())
        for lead in selected_leads
        if lead.contact
    }
    active_days = len(daily_counts)
    latest_selected = max(daily_counts, default=None)
    kpis = [
        {"label": "Leads", "value": f"{len(selected_leads):,}".replace(",", " "), "note": "dans la sélection"},
        {"label": "Contacts uniques", "value": f"{len(unique_contacts):,}".replace(",", " "), "note": "numéros renseignés"},
        {"label": "Jours actifs", "value": str(active_days), "note": "avec au moins un lead"},
        {
            "label": "Dernier lead",
            "value": latest_selected.strftime("%d/%m/%Y") if latest_selected else "—",
            "note": choice_labels[source],
        },
    ]

    paginator = Paginator(selected_leads, 75)
    page_obj = paginator.get_page(request.GET.get("page"))
    preserved_query = urlencode(
        {
            "source": source,
            "start": range_start.isoformat(),
            "end": range_end.isoformat(),
            "q": query,
        }
    )
    return render(
        request,
        "reporting/data_sheet.html",
        {
            "active_page": "data_sheet",
            "source": source,
            "source_label": choice_labels[source],
            "source_choices": choices,
            "range_start": range_start,
            "range_end": range_end,
            "query": query,
            "dataset": dataset,
            "connection_error": connection_error,
            "page_obj": page_obj,
            "lead_count": len(selected_leads),
            "kpis": kpis,
            "trend": trend,
            "top_statuses": top_statuses,
            "source_breakdown": source_breakdown,
            "preserved_query": preserved_query,
            "is_consolidated": source == "all",
            "show_client_column": source != "clicks",
            "show_source_column": source == "all",
            "channel_label": {
                "all": "Service / source",
                "landing": "Service",
                "clicks": "Source",
                "dossiers": "Type client",
            }[source],
            "details_label": {
                "all": "Information utile",
                "landing": "Société / observation",
                "clicks": "Formulaire / observation",
                "dossiers": "Pays / origine",
            }[source],
        },
    )


@login_required
def performance(request):
    connection_obj, all_selected = _selected_reporting_scope(request)
    connections = _reporting_connections(connection_obj, all_selected)
    accounts = _reporting_accounts(connections)
    range_start, range_end = _selected_range_for_accounts(accounts, request)
    end = _parse_date(request.GET.get("end"), range_end)
    start = _parse_date(request.GET.get("start"), end - timedelta(days=6))
    level = request.GET.get("level", InsightLevel.CAMPAIGN)
    if level not in InsightLevel.values:
        level = InsightLevel.CAMPAIGN
    query = request.GET.get("q", "").strip()
    rows = InsightDaily.objects.none()
    if accounts:
        rows = InsightDaily.objects.select_related("account").filter(account__in=accounts, level=level, date__range=(start, end)).order_by("-date", "-spend")
        if query:
            rows = rows.filter(Q(object_name__icontains=query) | Q(object_external_id__icontains=query))
    rows = list(rows[:500])
    return render(
        request,
        "reporting/performance.html",
        {
            "active_page": "performance",
            "account": accounts[0] if len(accounts) == 1 else None,
            "combined_accounts": all_selected,
            "rows": rows,
            "level": level,
            "levels": InsightLevel.choices,
            "start": start,
            "end": end,
            "query": query,
            **_navigation_context(connection_obj, allow_all=True, all_selected=all_selected),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def reports(request):
    connection_obj, all_selected = _selected_reporting_scope(request)
    connections = _reporting_connections(connection_obj, all_selected)
    accounts = _reporting_accounts(connections)
    account = accounts[0] if len(accounts) == 1 and not all_selected else None
    if request.method == "POST":
        if not accounts:
            messages.error(request, "Configurez et synchronisez d’abord au moins un compte Meta.")
            return redirect("reports")
        _, fallback_end = _selected_range_for_accounts(accounts, request)
        end = _parse_date(request.POST.get("end"), fallback_end)
        start = _parse_date(request.POST.get("start"), end)
        if start > end or (end - start).days > 365:
            messages.error(request, "La période demandée est invalide ou dépasse 365 jours.")
            return redirect("reports")
        if all_selected:
            version = generate_portfolio_report(accounts, start, end, source="manual", user=request.user)
        else:
            version = generate_report(accounts[0], start, end, source="manual", user=request.user)
        record_audit(
            action="report.generated",
            user=request.user,
            entity=version,
            metadata={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "scope": "portfolio" if all_selected else "account",
                "account_ids": [item.pk for item in accounts],
            },
            request=request,
        )
        messages.success(request, f"Rapport{' consolidé' if all_selected else ''} v{version.version} généré avec succès.")
        return redirect("reports")
    runs_query = ReportRun.objects.select_related("account", "created_by").prefetch_related("versions", "included_accounts")
    if all_selected:
        runs = runs_query.filter(scope=ReportScope.PORTFOLIO)[:100]
    elif account:
        runs = runs_query.filter(scope=ReportScope.ACCOUNT, account=account)[:100]
    else:
        runs = ReportRun.objects.none()
    _, default_end = _selected_range_for_accounts(accounts, request)
    return render(
        request,
        "reporting/reports.html",
        {
            "active_page": "reports",
            "account": account,
            "combined_accounts": all_selected,
            "portfolio_account_count": len(accounts),
            "runs": runs,
            "default_end": default_end,
            **_navigation_context(connection_obj, allow_all=True, all_selected=all_selected),
        },
    )


@login_required
def download_report(request, version_id, format_name):
    version = get_object_or_404(ReportVersion.objects.select_related("report_run"), pk=version_id)
    if format_name == "pdf" and version.pdf_file:
        field = version.pdf_file
        content_type = "application/pdf"
    elif format_name in {"excel", "xlsx"} and version.excel_file:
        field = version.excel_file
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        raise Http404("Format indisponible")
    record_audit(action="report.downloaded", user=request.user, entity=version, metadata={"format": format_name}, request=request)
    return FileResponse(field.open("rb"), as_attachment=True, filename=field.name.rsplit("/", 1)[-1], content_type=content_type)


@login_required
@require_http_methods(["GET", "POST"])
def settings_page(request):
    selected_connection = _selected_connection(request)
    editing_new = request.GET.get("new") == "1"
    connection_obj = MetaConnection() if editing_new else (selected_connection or MetaConnection())
    app_settings = AppSettings.load()
    connection_form = MetaConnectionForm(instance=connection_obj, prefix="connection")
    settings_form = AppSettingsForm(instance=app_settings, prefix="app")
    mapping_form = MetricMappingForm(prefix="mapping")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_connection":
            connection_form = MetaConnectionForm(request.POST, instance=connection_obj, prefix="connection")
            if connection_form.is_valid():
                obj = connection_form.save()
                request.session["selected_meta_connection_id"] = obj.pk
                record_audit(action="meta.connection.saved", user=request.user, entity=obj, request=request)
                messages.success(request, "Connexion Meta enregistrée. Testez-la avant la première synchronisation.")
                return redirect(f"{reverse('settings')}?connection={obj.pk}")
        elif action == "save_settings":
            settings_form = AppSettingsForm(request.POST, instance=app_settings, prefix="app")
            if settings_form.is_valid():
                obj = settings_form.save()
                record_audit(action="settings.updated", user=request.user, entity=obj, request=request)
                messages.success(request, "Planification et seuils enregistrés.")
                return redirect("settings")
        elif action == "save_mapping":
            if not connection_obj.pk:
                messages.error(request, "Enregistrez d’abord la connexion Meta.")
            else:
                mapping_form = MetricMappingForm(request.POST, prefix="mapping")
                if mapping_form.is_valid():
                    mapping = mapping_form.save(commit=False, user=request.user)
                    mapping.connection = connection_obj
                    mapping.save()
                    record_audit(action="metric_mapping.saved", user=request.user, entity=mapping, request=request)
                    messages.success(request, "Mesure Meta enregistrée.")
                    return redirect(f"{reverse('settings')}?connection={connection_obj.pk}")
    mappings = connection_obj.metric_mappings.all() if connection_obj.pk else MetricMapping.objects.none()
    discovered_actions = (
        ActionMetricDaily.objects.filter(insight__account__connection=connection_obj).values_list("action_type", flat=True).distinct().order_by("action_type")
        if connection_obj.pk
        else []
    )
    return render(
        request,
        "reporting/settings.html",
        {
            "active_page": "settings",
            "connection_obj": connection_obj,
            "connection_form": connection_form,
            "settings_form": settings_form,
            "mapping_form": mapping_form,
            "mappings": mappings,
            "discovered_actions": discovered_actions,
            "app_settings": app_settings,
            "editing_new": editing_new,
            **_navigation_context(selected_connection),
        },
    )


def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None


@login_required
@require_POST
def api_meta_test(request):
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "JSON invalide."}, status=400)
    connection_obj = _selected_connection(request, payload)
    if not connection_obj:
        return JsonResponse({"ok": False, "error": "Connexion Meta non configurée."}, status=400)
    health = MetaMarketingConnector(connection_obj).health_status()
    connection_obj.status = ConnectionStatus.CONNECTED if health.ok else ConnectionStatus.ERROR
    connection_obj.last_tested_at = timezone.now()
    connection_obj.last_error = "" if health.ok else health.message
    connection_obj.save(update_fields=["status", "last_tested_at", "last_error", "updated_at"])
    record_audit(action="meta.connection.tested", user=request.user, entity=connection_obj, metadata={"ok": health.ok}, request=request)
    response = {"ok": health.ok, "message": health.message, "details": health.details}
    if health.ok and connection_obj.is_active:
        app_settings = AppSettings.load()
        backfill_end = timezone.localdate() - timedelta(days=1)
        backfill_start = backfill_end - timedelta(days=app_settings.history_backfill_days - 1)
        completed = _backfill_sync(connection_obj, app_settings.history_backfill_days, FINISHED_SYNC_STATUSES)
        if not completed:
            active = _backfill_sync(connection_obj, app_settings.history_backfill_days, ACTIVE_SYNC_STATUSES)
            if active:
                run, created = active, False
            else:
                run, created = _queue_sync(
                    connection_obj,
                    backfill_start,
                    backfill_end,
                    trigger="initial_backfill",
                )
            response["sync"] = {"id": run.pk, "status": run.status, "created": created}
            response["message"] = (
                f"{health.message} Import automatique des {app_settings.history_backfill_days} derniers jours "
                f"{'démarré' if created else 'déjà en cours'}."
            )
            if created:
                record_audit(
                    action="meta.sync.requested",
                    user=request.user,
                    entity=run,
                    metadata={"levels": SYNC_LEVELS, "automatic": True, "reason": "initial_backfill"},
                    request=request,
                )
    return JsonResponse(response, status=200 if health.ok else 400)


@login_required
@require_http_methods(["GET", "POST"])
def api_syncs(request):
    if request.method == "GET":
        connection_obj = _selected_connection(request)
        data = [
            {"id": run.pk, "status": run.status, "start": run.requested_start, "end": run.requested_end, "records": run.records_count, "message": run.message}
            for run in SyncRun.objects.filter(connection=connection_obj)[:50]
        ]
        return JsonResponse({"data": data})
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"error": "JSON invalide."}, status=400)
    connection_obj = _selected_connection(request, payload)
    if not connection_obj:
        return JsonResponse({"error": "Connexion Meta non configurée."}, status=400)
    if not connection_obj.is_active:
        return JsonResponse({"error": "Cette connexion Meta est désactivée."}, status=400)
    end = _parse_date(payload.get("end"), timezone.localdate() - timedelta(days=1))
    start = _parse_date(payload.get("start"), end)
    if start > end or (end - start).days > 365:
        return JsonResponse({"error": "Période invalide ou supérieure à 365 jours."}, status=400)
    levels = payload.get("levels") or SYNC_LEVELS
    if any(level not in InsightLevel.values for level in levels):
        return JsonResponse({"error": "Niveau d’insight invalide."}, status=400)
    automatic = payload.get("automatic") is True
    run, created = _queue_sync(
        connection_obj,
        start,
        end,
        trigger="automatic_range" if automatic else "manual",
        levels=levels,
        generate_report=payload.get("generate_report", False),
    )
    if created:
        record_audit(
            action="meta.sync.requested",
            user=request.user,
            entity=run,
            metadata={"levels": levels, "automatic": automatic},
            request=request,
        )
    return JsonResponse(
        {"id": run.pk, "task_id": run.task_id, "status": run.status, "created": created},
        status=202,
    )


@login_required
@require_GET
def api_sync_detail(request, sync_run_id):
    run = get_object_or_404(SyncRun, pk=sync_run_id)
    return JsonResponse(
        {
            "id": run.pk,
            "status": run.status,
            "start": run.requested_start,
            "end": run.requested_end,
            "attempt": run.attempt,
            "records": run.records_count,
            "raw_pages": run.raw_pages_count,
            "message": run.message,
            "errors": [{"level": error.level, "code": error.code, "message": error.user_message or error.message} for error in run.errors.all()],
        }
    )


@login_required
@require_GET
def api_dashboard(request):
    connection_obj, all_selected = _selected_reporting_scope(request)
    accounts = _reporting_accounts(_reporting_connections(connection_obj, all_selected))
    range_start, range_end = _selected_range_for_accounts(accounts, request)
    payload = _dashboard_payload(accounts if all_selected else (accounts[0] if accounts else None), range_start, range_end, combined=all_selected)
    payload.pop("current", None)
    if payload.get("last_sync"):
        run = payload["last_sync"]
        payload["last_sync"] = {"id": run.pk, "status": run.status, "finished_at": run.finished_at}
    return JsonResponse(payload)


@login_required
@require_GET
def api_insights(request):
    connection_obj, all_selected = _selected_reporting_scope(request)
    accounts = _reporting_accounts(_reporting_connections(connection_obj, all_selected))
    if not accounts:
        return JsonResponse({"data": []})
    level = request.GET.get("level", InsightLevel.CAMPAIGN)
    if level not in InsightLevel.values:
        return JsonResponse({"error": "Niveau invalide."}, status=400)
    _, default_end = _selected_range_for_accounts(accounts, request)
    end = _parse_date(request.GET.get("end"), default_end)
    start = _parse_date(request.GET.get("start"), end)
    rows = InsightDaily.objects.select_related("account").filter(account__in=accounts, level=level, date__range=(start, end)).order_by("date", "object_name")[:2000]
    data = [
        {
            "id": row.pk,
            "date": row.date,
            "level": row.level,
            "object_id": row.object_external_id,
            "name": row.object_name,
            "account_id": row.account.external_id,
            "account_name": row.account.name,
            "spend": row.spend,
            "results": row.results,
            "cost_per_result": row.cost_per_result,
            "impressions": row.impressions,
            "reach": row.reach,
            "clicks": row.clicks,
            "ctr": row.ctr,
            "result_action_type": row.result_action_type,
            "result_verified": row.result_verified,
            "raw_payload_id": row.raw_payload_id,
        }
        for row in rows
    ]
    return JsonResponse({"data": data})


@login_required
@require_http_methods(["GET", "POST"])
def api_reports(request):
    if request.method == "GET":
        connection_obj, all_selected = _selected_reporting_scope(request)
        accounts = _reporting_accounts(_reporting_connections(connection_obj, all_selected))
        account = accounts[0] if len(accounts) == 1 and not all_selected else None
        if all_selected:
            runs = ReportRun.objects.filter(scope=ReportScope.PORTFOLIO)[:100]
        elif account:
            runs = ReportRun.objects.filter(scope=ReportScope.ACCOUNT, account=account)[:100]
        else:
            runs = []
        data = [
            {
                "id": run.pk,
                "scope": run.scope,
                "start": run.date_start,
                "end": run.date_end,
                "status": run.status,
                "versions": run.versions.count(),
            }
            for run in runs
        ]
        return JsonResponse({"data": data})
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"error": "JSON invalide."}, status=400)
    all_selected = str(payload.get("connection_id", "")) == "all"
    if all_selected:
        request.session[REPORTING_ALL_SESSION_KEY] = True
        connection_obj = None
    else:
        request.session[REPORTING_ALL_SESSION_KEY] = False
        connection_obj = _selected_connection(request, payload)
    accounts = _reporting_accounts(_reporting_connections(connection_obj, all_selected))
    if not accounts:
        return JsonResponse({"error": "Aucun compte Meta synchronisé."}, status=400)
    _, fallback_end = _selected_range_for_accounts(accounts, request)
    end = _parse_date(payload.get("end"), fallback_end)
    start = _parse_date(payload.get("start"), end)
    if start > end or (end - start).days > 365:
        return JsonResponse({"error": "Période invalide ou supérieure à 365 jours."}, status=400)
    version = (
        generate_portfolio_report(accounts, start, end, source="api", user=request.user)
        if all_selected
        else generate_report(accounts[0], start, end, source="api", user=request.user)
    )
    return JsonResponse(
        {
            "id": version.pk,
            "report_run_id": version.report_run_id,
            "scope": version.report_run.scope,
            "version": version.version,
            "snapshot_hash": version.snapshot_hash,
            "pdf": reverse("download_report", args=[version.pk, "pdf"]),
            "excel": reverse("download_report", args=[version.pk, "excel"]),
        },
        status=201,
    )


@login_required
@require_GET
def api_report_download(request, version_id):
    return download_report(request, version_id, request.GET.get("format", "pdf"))


@login_required
@require_http_methods(["GET", "PATCH"])
def api_settings(request):
    app_settings = AppSettings.load()
    connection_obj = _selected_connection(request)
    if request.method == "GET":
        return JsonResponse(
            {
                "schedule_time": app_settings.schedule_time.strftime("%H:%M"),
                "schedule_timezone": app_settings.schedule_timezone,
                "history_backfill_days": app_settings.history_backfill_days,
                "rolling_resync_days": app_settings.rolling_resync_days,
                "thresholds": {
                    "zero_result_spend_cpl_multiplier": app_settings.zero_result_spend_cpl_multiplier,
                    "cpl_increase_percent": app_settings.cpl_increase_percent,
                    "result_drop_percent": app_settings.result_drop_percent,
                    "spend_change_percent": app_settings.spend_change_percent,
                    "ctr_change_percent": app_settings.ctr_change_percent,
                    "frequency_threshold": app_settings.frequency_threshold,
                },
                "meta": {
                    "connection_id": connection_obj.pk if connection_obj else None,
                    "configured": bool(connection_obj and connection_obj.access_token_encrypted),
                    "status": connection_obj.status if connection_obj else "not_configured",
                    "account_id": connection_obj.ad_account_external_id if connection_obj else "",
                    "api_version": connection_obj.api_version if connection_obj else "v25.0",
                    "masked_token": connection_obj.masked_token if connection_obj else "Non configuré",
                },
                "meta_connections": [
                    {
                        "id": item.pk,
                        "name": item.name,
                        "account_id": item.ad_account_external_id,
                        "status": item.status,
                        "active": item.is_active,
                    }
                    for item in MetaConnection.objects.order_by("name", "id")
                ],
            }
        )
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"error": "JSON invalide."}, status=400)
    allowed = {
        "schedule_time",
        "schedule_timezone",
        "history_backfill_days",
        "rolling_resync_days",
        "zero_result_spend_cpl_multiplier",
        "cpl_increase_percent",
        "result_drop_percent",
        "spend_change_percent",
        "ctr_change_percent",
        "frequency_threshold",
    }
    for field, value in payload.items():
        if field in allowed:
            if field == "schedule_time":
                try:
                    value = time.fromisoformat(str(value))
                except ValueError:
                    return JsonResponse({"error": "Heure de planification invalide."}, status=400)
            setattr(app_settings, field, value)
    app_settings.full_clean()
    app_settings.save()
    record_audit(action="settings.api_updated", user=request.user, entity=app_settings, request=request)
    return JsonResponse({"ok": True})


def health(request):
    response = {"status": "ok", "database": "ok", "service": "ultex-meta-reports"}
    status = 200
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        response.update({"status": "degraded", "database": "error"})
        status = 503
    return JsonResponse(response, status=status)
