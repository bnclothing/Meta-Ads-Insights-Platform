from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from celery import shared_task
from django.core.management import call_command
from django.utils import timezone

from reporting.connectors import MetaApiError
from reporting.models import Anomaly, AnomalyStatus, AppSettings, InsightLevel, MetaConnection, Severity, SyncRun, SyncStatus
from reporting.services.sync import perform_sync


RETRY_DELAYS = [60, 5 * 60, 30 * 60]


def _record_sync_failure(sync_run, exc):
    account = sync_run.account or sync_run.connection.ad_accounts.order_by("id").first()
    if not account:
        return
    code = str(getattr(exc, "code", "") or "")
    token_invalid = code == "190"
    rule_id = "TOKEN_INVALID" if token_invalid else "SYNC_FAILED"
    Anomaly.objects.update_or_create(
        account=account,
        date=sync_run.requested_end,
        level=InsightLevel.ACCOUNT,
        object_external_id=account.external_id,
        rule_id=rule_id,
        defaults={
            "object_name": account.name,
            "severity": Severity.CRITICAL,
            "title": "Jeton Meta invalide" if token_invalid else "Synchronisation Meta en échec",
            "description": (
                "Meta a rejeté le jeton d’accès configuré."
                if token_invalid
                else "La synchronisation n’a pas abouti après les tentatives automatiques autorisées."
            ),
            "recommendation": (
                "Remplacer le jeton système dans les paramètres puis tester la connexion."
                if token_invalid
                else "Contrôler la connexion sortante, la disponibilité de Meta et les erreurs du cycle avant une relance manuelle."
            ),
            "metrics": {"sync_run_id": sync_run.pk, "attempt": sync_run.attempt, "code": code},
            "status": AnomalyStatus.OPEN,
            "resolved_at": None,
        },
    )


@shared_task(bind=True, max_retries=3)
def synchronize_meta(self, sync_run_id: int, generate_report: bool = True):
    sync_run = SyncRun.objects.select_related("connection").get(pk=sync_run_id)
    sync_run.task_id = self.request.id or ""
    sync_run.attempt = self.request.retries + 1
    sync_run.save(update_fields=["task_id", "attempt", "updated_at"])
    try:
        perform_sync(sync_run)
    except MetaApiError as exc:
        if exc.retryable and self.request.retries < self.max_retries:
            sync_run.status = SyncStatus.PENDING
            sync_run.message = f"Nouvelle tentative planifiée ({self.request.retries + 2}/4)."
            sync_run.save(update_fields=["status", "message", "updated_at"])
            raise self.retry(exc=exc, countdown=RETRY_DELAYS[self.request.retries])
        _record_sync_failure(sync_run, exc)
        raise
    except Exception as exc:
        _record_sync_failure(sync_run, exc)
        raise
    if sync_run.account:
        Anomaly.objects.filter(
            account=sync_run.account,
            rule_id__in=["SYNC_FAILED", "TOKEN_INVALID"],
            status__in=[AnomalyStatus.OPEN, AnomalyStatus.ACKNOWLEDGED],
        ).update(status=AnomalyStatus.RESOLVED, resolved_at=timezone.now())
    if generate_report and sync_run.status == SyncStatus.SUCCESS:
        from reporting.services.reports import generate_report

        generate_report(sync_run.account, sync_run.requested_end, sync_run.requested_end, source="scheduled")
    return sync_run.pk


@shared_task
def daily_meta_cycle():
    app_settings = AppSettings.load()
    target = timezone.localdate() - timedelta(days=1)
    start = target - timedelta(days=app_settings.rolling_resync_days - 1)
    task_ids = []
    for connection in MetaConnection.objects.filter(is_active=True):
        sync_run = SyncRun.objects.create(
            connection=connection,
            requested_start=start,
            requested_end=target,
            trigger="scheduled",
            levels=["account", "campaign", "adset", "ad"],
        )
        result = synchronize_meta.delay(sync_run.pk, generate_report=True)
        task_ids.append(result.id)
    return task_ids


@shared_task
def scheduled_dispatch_tick():
    app_settings = AppSettings.load()
    now = timezone.now().astimezone(ZoneInfo(app_settings.schedule_timezone))
    if (now.hour, now.minute) != (app_settings.schedule_time.hour, app_settings.schedule_time.minute):
        return {"dispatched": False, "reason": "not_due"}
    target = now.date() - timedelta(days=1)
    if SyncRun.objects.filter(trigger="scheduled", requested_end=target).exclude(status=SyncStatus.FAILED).exists():
        return {"dispatched": False, "reason": "already_dispatched"}
    result = daily_meta_cycle.delay()
    return {"dispatched": True, "task_id": result.id}


@shared_task
def backup_database_task():
    call_command("backup_database")
    return {"ok": True}
