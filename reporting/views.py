from __future__ import annotations

import json
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from reporting.connectors import MetaMarketingConnector
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
    ReportVersion,
    SyncRun,
)
from reporting.services.audit import record_audit
from reporting.services.metrics import ZERO, aggregate_rows, percent_change
from reporting.services.reports import generate_report
from reporting.tasks import synchronize_meta


def _parse_date(value, fallback):
    try:
        return date.fromisoformat(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _active_account():
    connection_obj = MetaConnection.objects.filter(is_active=True).order_by("id").first()
    if not connection_obj:
        return None
    configured_id = connection_obj.ad_account_external_id.removeprefix("act_")
    accounts = AdAccount.objects.select_related("connection").filter(connection=connection_obj)
    return accounts.filter(external_id=configured_id).first() or accounts.order_by("-id").first()


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


def _display_decimal(value, places="0.01"):
    if value is None:
        return "—"
    quantized = Decimal(value).quantize(Decimal(places))
    return f"{quantized:,.{abs(quantized.as_tuple().exponent)}f}".replace(",", " ")


def _dashboard_payload(account, range_start, range_end):
    if not account:
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

    current_rows = list(
        InsightDaily.objects.filter(
            account=account,
            level=InsightLevel.ACCOUNT,
            date__range=(range_start, range_end),
        ).order_by("date")
    )
    period_days = (range_end - range_start).days + 1
    previous_end = range_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days - 1)
    previous_rows = list(
        InsightDaily.objects.filter(
            account=account,
            level=InsightLevel.ACCOUNT,
            date__range=(previous_start, previous_end),
        ).order_by("date")
    )
    current_summary = aggregate_rows(current_rows)
    previous_summary = aggregate_rows(previous_rows)

    if current_rows:
        latest_row = current_rows[-1]
        values = [
            (
                "Dépenses",
                current_summary["spend"],
                percent_change(current_summary["spend"], previous_summary["spend"]),
                account.currency,
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
                account.currency,
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
                display = "Manquant"
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

    campaign_rows = list(
        InsightDaily.objects.filter(
            account=account,
            level=InsightLevel.CAMPAIGN,
            date__range=(range_start, range_end),
        ).order_by("date", "object_external_id")
    )
    grouped_campaigns = {}
    for row in campaign_rows:
        group = grouped_campaigns.setdefault(
            row.object_external_id,
            {
                "id": row.object_external_id,
                "name": row.object_name,
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
        account=account,
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
    trend_qs = InsightDaily.objects.filter(
        account=account,
        level=InsightLevel.ACCOUNT,
        date__range=(range_start, range_end),
    ).order_by("date")
    trend = [
        {
            "date": row.date.isoformat(),
            "label": row.date.strftime("%d/%m"),
            "spend": float(row.spend),
            "results": float(row.results) if row.results is not None else None,
        }
        for row in trend_qs
    ]
    last_sync = (
        SyncRun.objects.filter(account=account, requested_end__gte=range_start, requested_start__lte=range_end)
        .order_by("-created_at")
        .first()
    )
    return {
        "account": {"id": account.pk, "name": account.name, "currency": account.currency, "timezone": account.timezone_name},
        "date": range_end.isoformat(),
        "start": range_start.isoformat(),
        "end": range_end.isoformat(),
        "current": bool(current_rows),
        "kpis": kpis,
        "campaigns": campaigns,
        "alerts": alerts,
        "trend": trend,
        "last_sync": last_sync,
        "data_state": "complete" if current_rows and (not last_sync or last_sync.status == "success") else "missing",
    }


@login_required
def dashboard(request):
    account = _active_account()
    connection_obj = MetaConnection.objects.filter(is_active=True).order_by("id").first()
    app_settings = AppSettings.load()
    range_start, range_end = _selected_range(account, request)
    context = _dashboard_payload(account, range_start, range_end)
    context.update(
        {
            "active_page": "dashboard",
            "selected_start": range_start,
            "selected_end": range_end,
            "connection_ready": bool(connection_obj and connection_obj.status == ConnectionStatus.CONNECTED),
            "backfill_days": app_settings.history_backfill_days,
            "backfill_start": range_end - timedelta(days=app_settings.history_backfill_days - 1),
        }
    )
    return render(request, "reporting/dashboard.html", context)


@login_required
def performance(request):
    account = _active_account()
    end = _parse_date(request.GET.get("end"), _selected_date(account, request))
    start = _parse_date(request.GET.get("start"), end - timedelta(days=6))
    level = request.GET.get("level", InsightLevel.CAMPAIGN)
    if level not in InsightLevel.values:
        level = InsightLevel.CAMPAIGN
    query = request.GET.get("q", "").strip()
    rows = InsightDaily.objects.none()
    if account:
        rows = InsightDaily.objects.filter(account=account, level=level, date__range=(start, end)).order_by("-date", "-spend")
        if query:
            rows = rows.filter(Q(object_name__icontains=query) | Q(object_external_id__icontains=query))
    rows = list(rows[:500])
    return render(
        request,
        "reporting/performance.html",
        {
            "active_page": "performance",
            "account": account,
            "rows": rows,
            "level": level,
            "levels": InsightLevel.choices,
            "start": start,
            "end": end,
            "query": query,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def reports(request):
    account = _active_account()
    if request.method == "POST":
        if not account:
            messages.error(request, "Configurez et synchronisez d’abord le compte Meta.")
            return redirect("reports")
        end = _parse_date(request.POST.get("end"), _selected_date(account, request))
        start = _parse_date(request.POST.get("start"), end)
        if start > end or (end - start).days > 365:
            messages.error(request, "La période demandée est invalide ou dépasse 365 jours.")
            return redirect("reports")
        version = generate_report(account, start, end, source="manual", user=request.user)
        record_audit(action="report.generated", user=request.user, entity=version, metadata={"start": start.isoformat(), "end": end.isoformat()}, request=request)
        messages.success(request, f"Rapport v{version.version} généré avec succès.")
        return redirect("reports")
    runs = ReportRun.objects.select_related("account", "created_by").prefetch_related("versions").all()[:100]
    default_end = _selected_date(account, request)
    return render(request, "reporting/reports.html", {"active_page": "reports", "account": account, "runs": runs, "default_end": default_end})


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
    connection_obj = MetaConnection.objects.order_by("id").first() or MetaConnection()
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
                record_audit(action="meta.connection.saved", user=request.user, entity=obj, request=request)
                messages.success(request, "Connexion Meta enregistrée. Testez-la avant la première synchronisation.")
                return redirect("settings")
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
                    return redirect("settings")
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
    connection_obj = MetaConnection.objects.order_by("id").first()
    if not connection_obj:
        return JsonResponse({"ok": False, "error": "Connexion Meta non configurée."}, status=400)
    health = MetaMarketingConnector(connection_obj).health_status()
    connection_obj.status = ConnectionStatus.CONNECTED if health.ok else ConnectionStatus.ERROR
    connection_obj.last_tested_at = timezone.now()
    connection_obj.last_error = "" if health.ok else health.message
    connection_obj.save(update_fields=["status", "last_tested_at", "last_error", "updated_at"])
    record_audit(action="meta.connection.tested", user=request.user, entity=connection_obj, metadata={"ok": health.ok}, request=request)
    return JsonResponse({"ok": health.ok, "message": health.message, "details": health.details}, status=200 if health.ok else 400)


@login_required
@require_http_methods(["GET", "POST"])
def api_syncs(request):
    if request.method == "GET":
        data = [
            {"id": run.pk, "status": run.status, "start": run.requested_start, "end": run.requested_end, "records": run.records_count, "message": run.message}
            for run in SyncRun.objects.all()[:50]
        ]
        return JsonResponse({"data": data})
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"error": "JSON invalide."}, status=400)
    connection_obj = MetaConnection.objects.filter(is_active=True).order_by("id").first()
    if not connection_obj:
        return JsonResponse({"error": "Connexion Meta non configurée."}, status=400)
    end = _parse_date(payload.get("end"), timezone.localdate() - timedelta(days=1))
    start = _parse_date(payload.get("start"), end)
    if start > end or (end - start).days > 365:
        return JsonResponse({"error": "Période invalide ou supérieure à 365 jours."}, status=400)
    levels = payload.get("levels") or list(InsightLevel.values)
    if any(level not in InsightLevel.values for level in levels):
        return JsonResponse({"error": "Niveau d’insight invalide."}, status=400)
    run = SyncRun.objects.create(connection=connection_obj, requested_start=start, requested_end=end, trigger="manual", levels=levels)
    task = synchronize_meta.delay(run.pk, generate_report=payload.get("generate_report", False))
    run.task_id = task.id or ""
    run.save(update_fields=["task_id", "updated_at"])
    record_audit(action="meta.sync.requested", user=request.user, entity=run, metadata={"levels": levels}, request=request)
    return JsonResponse({"id": run.pk, "task_id": run.task_id, "status": run.status}, status=202)


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
    account = _active_account()
    range_start, range_end = _selected_range(account, request)
    payload = _dashboard_payload(account, range_start, range_end)
    payload.pop("current", None)
    if payload.get("last_sync"):
        run = payload["last_sync"]
        payload["last_sync"] = {"id": run.pk, "status": run.status, "finished_at": run.finished_at}
    return JsonResponse(payload)


@login_required
@require_GET
def api_insights(request):
    account = _active_account()
    if not account:
        return JsonResponse({"data": []})
    level = request.GET.get("level", InsightLevel.CAMPAIGN)
    if level not in InsightLevel.values:
        return JsonResponse({"error": "Niveau invalide."}, status=400)
    end = _parse_date(request.GET.get("end"), _selected_date(account, request))
    start = _parse_date(request.GET.get("start"), end)
    rows = InsightDaily.objects.filter(account=account, level=level, date__range=(start, end)).order_by("date", "object_name")[:2000]
    data = [
        {
            "id": row.pk,
            "date": row.date,
            "level": row.level,
            "object_id": row.object_external_id,
            "name": row.object_name,
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
        data = [
            {"id": run.pk, "start": run.date_start, "end": run.date_end, "status": run.status, "versions": run.versions.count()}
            for run in ReportRun.objects.all()[:100]
        ]
        return JsonResponse({"data": data})
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"error": "JSON invalide."}, status=400)
    account = _active_account()
    if not account:
        return JsonResponse({"error": "Aucun compte Meta synchronisé."}, status=400)
    end = _parse_date(payload.get("end"), _selected_date(account, request))
    start = _parse_date(payload.get("start"), end)
    if start > end or (end - start).days > 365:
        return JsonResponse({"error": "Période invalide ou supérieure à 365 jours."}, status=400)
    version = generate_report(account, start, end, source="api", user=request.user)
    return JsonResponse(
        {
            "id": version.pk,
            "report_run_id": version.report_run_id,
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
    connection_obj = MetaConnection.objects.order_by("id").first()
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
                    "configured": bool(connection_obj and connection_obj.access_token_encrypted),
                    "status": connection_obj.status if connection_obj else "not_configured",
                    "account_id": connection_obj.ad_account_external_id if connection_obj else "",
                    "api_version": connection_obj.api_version if connection_obj else "v25.0",
                    "masked_token": connection_obj.masked_token if connection_obj else "Non configuré",
                },
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
