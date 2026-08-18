from django.urls import path

from . import views


urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("performance/", views.performance, name="performance"),
    path("rapports/", views.reports, name="reports"),
    path("rapports/<int:version_id>/telecharger/<str:format_name>/", views.download_report, name="download_report"),
    path("parametres/", views.settings_page, name="settings"),
    path("api/v1/meta/test", views.api_meta_test, name="api_meta_test"),
    path("api/v1/syncs", views.api_syncs, name="api_syncs"),
    path("api/v1/syncs/<int:sync_run_id>", views.api_sync_detail, name="api_sync_detail"),
    path("api/v1/dashboard", views.api_dashboard, name="api_dashboard"),
    path("api/v1/insights", views.api_insights, name="api_insights"),
    path("api/v1/reports", views.api_reports, name="api_reports"),
    path("api/v1/reports/<int:version_id>/download", views.api_report_download, name="api_report_download"),
    path("api/v1/settings", views.api_settings, name="api_settings"),
    path("health", views.health, name="health"),
]
