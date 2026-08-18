from django import forms
from django.utils import timezone

from .models import AppSettings, MetaConnection, MetricMapping


class MetaConnectionForm(forms.ModelForm):
    access_token = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "placeholder": "Laisser vide pour conserver le jeton"}),
        label="Jeton d’accès système",
    )
    app_secret = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "placeholder": "Laisser vide pour conserver le secret"}),
        label="Secret de l’application",
    )

    class Meta:
        model = MetaConnection
        fields = ("name", "app_id", "ad_account_external_id", "api_version", "is_active")
        labels = {
            "name": "Nom de la connexion",
            "app_id": "Identifiant de l’application Meta",
            "ad_account_external_id": "Identifiant du compte publicitaire",
            "api_version": "Version de l’API",
            "is_active": "Connexion active",
        }

    def clean_ad_account_external_id(self):
        value = self.cleaned_data["ad_account_external_id"].strip()
        return value.removeprefix("act_")

    def save(self, commit=True):
        instance = super().save(commit=False)
        token = self.cleaned_data.get("access_token")
        app_secret = self.cleaned_data.get("app_secret")
        if token:
            instance.set_access_token(token.strip())
        if app_secret:
            instance.set_app_secret(app_secret.strip())
        if commit:
            instance.save()
        return instance


class AppSettingsForm(forms.ModelForm):
    class Meta:
        model = AppSettings
        fields = (
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
        )
        widgets = {"schedule_time": forms.TimeInput(attrs={"type": "time"})}


class MetricMappingForm(forms.ModelForm):
    class Meta:
        model = MetricMapping
        fields = ("scope_type", "scope_value", "action_type", "label", "is_verified", "is_active")

    def save(self, commit=True, user=None):
        instance = super().save(commit=False)
        if instance.is_verified:
            instance.verified_by = user
            instance.verified_at = timezone.now()
        if commit:
            instance.save()
        return instance

