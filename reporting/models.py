from __future__ import annotations

from datetime import time
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from .security import decrypt_secret, encrypt_secret, secret_fingerprint


class LocalUser(User):
    class Meta:
        proxy = True
        verbose_name = "utilisateur local"
        verbose_name_plural = "utilisateurs locaux"


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ConnectionStatus(models.TextChoices):
    NOT_CONFIGURED = "not_configured", "Non configurée"
    CONNECTED = "connected", "Connectée"
    ERROR = "error", "En erreur"


class SyncStatus(models.TextChoices):
    PENDING = "pending", "En attente"
    RUNNING = "running", "En cours"
    SUCCESS = "success", "Réussie"
    PARTIAL = "partial", "Partielle"
    FAILED = "failed", "Échouée"


class InsightLevel(models.TextChoices):
    ACCOUNT = "account", "Compte"
    CAMPAIGN = "campaign", "Campagne"
    ADSET = "adset", "Ensemble"
    AD = "ad", "Publicité"


class Severity(models.TextChoices):
    CRITICAL = "critical", "Critique"
    HIGH = "high", "Élevée"
    MEDIUM = "medium", "Moyenne"
    INFO = "info", "Informative"


class AnomalyStatus(models.TextChoices):
    OPEN = "open", "Ouverte"
    ACKNOWLEDGED = "acknowledged", "Prise en compte"
    RESOLVED = "resolved", "Résolue"
    IGNORED = "ignored", "Ignorée"


class ReportStatus(models.TextChoices):
    PENDING = "pending", "En attente"
    GENERATING = "generating", "Génération"
    READY = "ready", "Prêt"
    PARTIAL = "partial", "Partiel"
    FAILED = "failed", "Échec"


class MetaConnection(TimestampedModel):
    name = models.CharField(max_length=120, default="Compte Meta ULTEx")
    app_id = models.CharField(max_length=80, blank=True)
    ad_account_external_id = models.CharField(max_length=80, blank=True, help_text="Identifiant sans le préfixe act_")
    api_version = models.CharField(max_length=20, default="v25.0")
    access_token_encrypted = models.TextField(blank=True, editable=False)
    app_secret_encrypted = models.TextField(blank=True, editable=False)
    access_token_fingerprint = models.CharField(max_length=20, blank=True, editable=False)
    token_last_four = models.CharField(max_length=4, blank=True, editable=False)
    is_active = models.BooleanField(default=True)
    status = models.CharField(max_length=24, choices=ConnectionStatus.choices, default=ConnectionStatus.NOT_CONFIGURED)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_successful_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def set_access_token(self, value: str):
        self.access_token_encrypted = encrypt_secret(value)
        self.access_token_fingerprint = secret_fingerprint(value)
        self.token_last_four = value[-4:] if value else ""

    def get_access_token(self) -> str:
        return decrypt_secret(self.access_token_encrypted)

    def set_app_secret(self, value: str):
        self.app_secret_encrypted = encrypt_secret(value)

    def get_app_secret(self) -> str:
        return decrypt_secret(self.app_secret_encrypted)

    @property
    def masked_token(self) -> str:
        return f"••••••••{self.token_last_four}" if self.token_last_four else "Non configuré"


class AdAccount(TimestampedModel):
    connection = models.ForeignKey(MetaConnection, on_delete=models.CASCADE, related_name="ad_accounts")
    external_id = models.CharField(max_length=80)
    name = models.CharField(max_length=200, blank=True)
    currency = models.CharField(max_length=12, default="MAD")
    timezone_name = models.CharField(max_length=80, default="Africa/Casablanca")
    timezone_offset_hours_utc = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    account_status = models.IntegerField(null=True, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["connection", "external_id"], name="uniq_connection_ad_account")]
        ordering = ["name", "external_id"]

    def __str__(self):
        return self.name or f"act_{self.external_id}"


class AdObjectSnapshot(TimestampedModel):
    account = models.ForeignKey(AdAccount, on_delete=models.CASCADE)
    external_id = models.CharField(max_length=80)
    name = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=40, blank=True)
    effective_status = models.CharField(max_length=40, blank=True)
    daily_budget = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    lifetime_budget = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    start_time = models.DateTimeField(null=True, blank=True)
    stop_time = models.DateTimeField(null=True, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        abstract = True


class CampaignSnapshot(AdObjectSnapshot):
    objective = models.CharField(max_length=80, blank=True)
    buying_type = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["account", "external_id"], name="uniq_campaign_account_external")]
        ordering = ["name"]

    def __str__(self):
        return self.name or self.external_id


class AdSetSnapshot(AdObjectSnapshot):
    campaign = models.ForeignKey(CampaignSnapshot, null=True, blank=True, on_delete=models.SET_NULL, related_name="adsets")
    campaign_external_id = models.CharField(max_length=80, blank=True)
    optimization_goal = models.CharField(max_length=100, blank=True)
    billing_event = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["account", "external_id"], name="uniq_adset_account_external")]
        ordering = ["name"]

    def __str__(self):
        return self.name or self.external_id


class AdSnapshot(AdObjectSnapshot):
    campaign = models.ForeignKey(CampaignSnapshot, null=True, blank=True, on_delete=models.SET_NULL, related_name="ads")
    adset = models.ForeignKey(AdSetSnapshot, null=True, blank=True, on_delete=models.SET_NULL, related_name="ads")
    campaign_external_id = models.CharField(max_length=80, blank=True)
    adset_external_id = models.CharField(max_length=80, blank=True)
    creative_external_id = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["account", "external_id"], name="uniq_ad_account_external")]
        ordering = ["name"]

    def __str__(self):
        return self.name or self.external_id


class SyncRun(TimestampedModel):
    connection = models.ForeignKey(MetaConnection, on_delete=models.CASCADE, related_name="sync_runs")
    account = models.ForeignKey(AdAccount, null=True, blank=True, on_delete=models.SET_NULL, related_name="sync_runs")
    requested_start = models.DateField()
    requested_end = models.DateField()
    trigger = models.CharField(max_length=24, default="manual")
    status = models.CharField(max_length=16, choices=SyncStatus.choices, default=SyncStatus.PENDING)
    task_id = models.CharField(max_length=120, blank=True)
    attempt = models.PositiveSmallIntegerField(default=1)
    levels = models.JSONField(default=list)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    records_count = models.PositiveIntegerField(default=0)
    raw_pages_count = models.PositiveIntegerField(default=0)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["connection", "status", "-created_at"])]


class RawApiPayload(models.Model):
    sync_run = models.ForeignKey(SyncRun, on_delete=models.CASCADE, related_name="raw_payloads")
    endpoint = models.CharField(max_length=300)
    request_params = models.JSONField(default=dict)
    payload = models.JSONField(default=dict)
    page_number = models.PositiveIntegerField(default=1)
    checksum = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sync_run", "id"]
        indexes = [models.Index(fields=["sync_run", "endpoint"]), models.Index(fields=["checksum"])]


class SyncError(models.Model):
    sync_run = models.ForeignKey(SyncRun, on_delete=models.CASCADE, related_name="errors")
    level = models.CharField(max_length=20, blank=True)
    code = models.CharField(max_length=40, blank=True)
    subcode = models.CharField(max_length=40, blank=True)
    message = models.TextField()
    user_message = models.TextField(blank=True)
    is_retryable = models.BooleanField(default=False)
    context = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class MetricMapping(TimestampedModel):
    SCOPE_CHOICES = [("global", "Global"), ("objective", "Objectif"), ("campaign", "Campagne")]
    connection = models.ForeignKey(MetaConnection, on_delete=models.CASCADE, related_name="metric_mappings")
    scope_type = models.CharField(max_length=20, choices=SCOPE_CHOICES, default="global")
    scope_value = models.CharField(max_length=120, default="*")
    action_type = models.CharField(max_length=180)
    label = models.CharField(max_length=100, default="Meta résultat")
    is_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["connection", "scope_type", "scope_value"], name="uniq_metric_mapping_scope")]
        ordering = ["scope_type", "scope_value"]


class InsightDaily(models.Model):
    account = models.ForeignKey(AdAccount, on_delete=models.CASCADE, related_name="insights")
    sync_run = models.ForeignKey(SyncRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="insights")
    raw_payload = models.ForeignKey(RawApiPayload, null=True, blank=True, on_delete=models.SET_NULL, related_name="insights")
    level = models.CharField(max_length=16, choices=InsightLevel.choices)
    object_external_id = models.CharField(max_length=80)
    object_name = models.CharField(max_length=300, blank=True)
    campaign_external_id = models.CharField(max_length=80, blank=True)
    adset_external_id = models.CharField(max_length=80, blank=True)
    ad_external_id = models.CharField(max_length=80, blank=True)
    date = models.DateField()
    currency = models.CharField(max_length=12)
    spend = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    impressions = models.PositiveBigIntegerField(default=0)
    reach = models.PositiveBigIntegerField(default=0)
    clicks = models.PositiveBigIntegerField(default=0)
    link_clicks = models.PositiveBigIntegerField(default=0)
    frequency = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    ctr = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    cpc = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    cpm = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    results = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    cost_per_result = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    result_action_type = models.CharField(max_length=180, blank=True)
    result_label = models.CharField(max_length=100, default="Meta résultat")
    result_verified = models.BooleanField(default=False)
    attribution_key = models.CharField(max_length=64, default="default")
    attribution_setting = models.JSONField(default=dict, blank=True)
    actions = models.JSONField(default=list, blank=True)
    cost_per_action_type = models.JSONField(default=list, blank=True)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "date", "level", "object_external_id", "attribution_key"],
                name="uniq_daily_insight_dimension",
            )
        ]
        ordering = ["date", "level", "object_name"]
        indexes = [
            models.Index(fields=["account", "date", "level"]),
            models.Index(fields=["campaign_external_id", "date"]),
            models.Index(fields=["adset_external_id", "date"]),
            models.Index(fields=["ad_external_id", "date"]),
        ]

    def __str__(self):
        return f"{self.date} · {self.level} · {self.object_name or self.object_external_id}"


class ActionMetricDaily(models.Model):
    insight = models.ForeignKey(InsightDaily, on_delete=models.CASCADE, related_name="action_metrics")
    action_type = models.CharField(max_length=180)
    value = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    cost = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["insight", "action_type"], name="uniq_action_metric_insight_type")]
        ordering = ["action_type"]


class Anomaly(TimestampedModel):
    account = models.ForeignKey(AdAccount, on_delete=models.CASCADE, related_name="anomalies")
    date = models.DateField()
    level = models.CharField(max_length=16, choices=InsightLevel.choices, default=InsightLevel.ACCOUNT)
    object_external_id = models.CharField(max_length=80)
    object_name = models.CharField(max_length=300, blank=True)
    rule_id = models.CharField(max_length=80)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    title = models.CharField(max_length=200)
    description = models.TextField()
    recommendation = models.TextField()
    metrics = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=AnomalyStatus.choices, default=AnomalyStatus.OPEN)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "date", "level", "object_external_id", "rule_id"],
                name="uniq_anomaly_rule_dimension",
            )
        ]
        ordering = ["-date", "severity", "object_name"]
        indexes = [models.Index(fields=["account", "status", "-date"])]


class ReportRun(TimestampedModel):
    account = models.ForeignKey(AdAccount, on_delete=models.CASCADE, related_name="reports")
    date_start = models.DateField()
    date_end = models.DateField()
    status = models.CharField(max_length=20, choices=ReportStatus.choices, default=ReportStatus.PENDING)
    source = models.CharField(max_length=20, default="manual")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-date_end", "-created_at"]
        indexes = [models.Index(fields=["account", "-date_end"])]
        constraints = [models.UniqueConstraint(fields=["account", "date_start", "date_end"], name="uniq_report_account_period")]


def report_upload_path(instance, filename):
    return f"reports/{instance.report_run_id}/{instance.version}/{filename}"


class ReportVersion(models.Model):
    report_run = models.ForeignKey(ReportRun, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    snapshot_hash = models.CharField(max_length=64)
    pdf_file = models.FileField(upload_to=report_upload_path, blank=True)
    excel_file = models.FileField(upload_to=report_upload_path, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["report_run", "version"], name="uniq_report_version")]
        ordering = ["-version"]


class AuditEvent(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=120)
    entity_type = models.CharField(max_length=120, blank=True)
    entity_id = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["action", "-created_at"])]


class AppSettings(TimestampedModel):
    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    schedule_time = models.TimeField(default=time(8, 0))
    schedule_timezone = models.CharField(max_length=80, default="Africa/Casablanca")
    history_backfill_days = models.PositiveSmallIntegerField(default=90, validators=[MinValueValidator(1), MaxValueValidator(1095)])
    rolling_resync_days = models.PositiveSmallIntegerField(default=28, validators=[MinValueValidator(1), MaxValueValidator(90)])
    zero_result_spend_cpl_multiplier = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("1.50"))
    cpl_increase_percent = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("50"))
    result_drop_percent = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("40"))
    spend_change_percent = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("30"))
    ctr_change_percent = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("30"))
    frequency_threshold = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("3.50"))
    backup_last_success_at = models.DateTimeField(null=True, blank=True)
    backup_status = models.CharField(max_length=30, default="not_run")

    class Meta:
        verbose_name = "paramètres de l'application"
        verbose_name_plural = "paramètres de l'application"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(singleton_key=1)
        return obj

    def save(self, *args, **kwargs):
        self.singleton_key = 1
        super().save(*args, **kwargs)
