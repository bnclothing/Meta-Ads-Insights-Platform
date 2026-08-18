from django.contrib import admin

from . import models


@admin.register(models.MetaConnection)
class MetaConnectionAdmin(admin.ModelAdmin):
    list_display = ("name", "ad_account_external_id", "api_version", "status", "last_successful_sync_at")
    readonly_fields = ("access_token_fingerprint", "token_last_four", "last_tested_at", "last_successful_sync_at", "last_error")
    exclude = ("access_token_encrypted", "app_secret_encrypted")


@admin.register(models.AdAccount)
class AdAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "external_id", "currency", "timezone_name", "last_synced_at")
    search_fields = ("name", "external_id")


@admin.register(models.InsightDaily)
class InsightDailyAdmin(admin.ModelAdmin):
    list_display = ("date", "level", "object_name", "spend", "results", "currency", "result_verified")
    list_filter = ("date", "level", "result_verified")
    search_fields = ("object_name", "object_external_id")


@admin.register(models.SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ("created_at", "connection", "requested_start", "requested_end", "status", "records_count")
    list_filter = ("status", "trigger")


@admin.register(models.Anomaly)
class AnomalyAdmin(admin.ModelAdmin):
    list_display = ("date", "severity", "rule_id", "object_name", "status")
    list_filter = ("severity", "status", "rule_id")


@admin.register(models.ReportRun)
class ReportRunAdmin(admin.ModelAdmin):
    list_display = ("date_start", "date_end", "account", "status", "source", "created_at")


admin.site.register(models.CampaignSnapshot)
admin.site.register(models.AdSetSnapshot)
admin.site.register(models.AdSnapshot)
admin.site.register(models.ActionMetricDaily)
admin.site.register(models.RawApiPayload)
admin.site.register(models.SyncError)
admin.site.register(models.MetricMapping)
admin.site.register(models.ReportVersion)
admin.site.register(models.AuditEvent)
admin.site.register(models.AppSettings)

