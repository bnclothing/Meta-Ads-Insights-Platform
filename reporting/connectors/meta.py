from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from django.conf import settings

from .base import DataSourceConnector, FetchedPage, HealthStatus


class MetaApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        subcode: str = "",
        user_message: str = "",
        retryable: bool = False,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.code = str(code or "")
        self.subcode = str(subcode or "")
        self.user_message = user_message or ""
        self.retryable = retryable
        self.status_code = status_code


Transport = Callable[[str, str, dict[str, Any]], dict[str, Any]]


@dataclass
class MetaMarketingConnector(DataSourceConnector):
    connection: Any
    transport: Transport | None = None
    poll_interval_seconds: float = 5.0
    async_timeout_seconds: int = 20 * 60

    INSIGHT_FIELDS = [
        "account_id",
        "account_name",
        "account_currency",
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "ad_id",
        "ad_name",
        "date_start",
        "date_stop",
        "spend",
        "impressions",
        "reach",
        "frequency",
        "clicks",
        "inline_link_clicks",
        "ctr",
        "cpc",
        "cpm",
        "actions",
        "cost_per_action_type",
        "conversion_leads",
        "cost_per_conversion_lead",
        "cost_per_result",
    ]

    OBJECT_FIELDS = {
        "campaigns": "id,name,objective,buying_type,status,effective_status,daily_budget,lifetime_budget,start_time,stop_time,updated_time",
        "adsets": "id,name,campaign_id,optimization_goal,billing_event,status,effective_status,daily_budget,lifetime_budget,start_time,end_time,updated_time",
        "ads": "id,name,campaign_id,adset_id,creative{id},status,effective_status,created_time,updated_time",
    }

    @property
    def account_node(self) -> str:
        account_id = self.connection.ad_account_external_id.removeprefix("act_")
        return f"act_{account_id}"

    @property
    def api_version(self) -> str:
        return self.connection.api_version or settings.META_GRAPH_API_VERSION

    @property
    def api_root(self) -> str:
        return f"{settings.META_GRAPH_API_BASE.rstrip('/')}/{self.api_version}"

    def _auth_params(self) -> dict[str, str]:
        token = self.connection.get_access_token()
        if not token:
            raise MetaApiError("Aucun jeton Meta n’est configuré.", code="missing_token")
        params = {"access_token": token}
        app_secret = self.connection.get_app_secret()
        if app_secret:
            params["appsecret_proof"] = hmac.new(app_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
        return params

    @staticmethod
    def _sanitized_params(params: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in params.items() if key not in {"access_token", "appsecret_proof"}}

    @staticmethod
    def _sanitized_url(url: str) -> str:
        parsed = urlparse(url)
        query = [(key, value) for key, value in parse_qsl(parsed.query) if key not in {"access_token", "appsecret_proof"}]
        return urlunparse(parsed._replace(query=urlencode(query)))

    @classmethod
    def _sanitized_payload(cls, value):
        if isinstance(value, dict):
            sanitized = {}
            for key, item in value.items():
                if key in {"access_token", "appsecret_proof"}:
                    sanitized[key] = "[redacted]"
                elif key in {"next", "previous"} and isinstance(item, str):
                    sanitized[key] = cls._sanitized_url(item)
                else:
                    sanitized[key] = cls._sanitized_payload(item)
            return sanitized
        if isinstance(value, list):
            return [cls._sanitized_payload(item) for item in value]
        return value

    def _default_transport(self, method: str, url: str, params: dict[str, Any]) -> dict[str, Any]:
        encoded = urlencode(params, doseq=True).encode("utf-8")
        if method == "GET":
            separator = "&" if "?" in url else "?"
            request = Request(f"{url}{separator}{encoded.decode('utf-8')}", method="GET", headers={"Accept": "application/json"})
        else:
            request = Request(url, data=encoded, method=method, headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {"error": {"message": f"Meta HTTP {exc.code}"}}
            raise self._error_from_payload(payload, status_code=exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise MetaApiError("La connexion réseau à Meta a échoué.", code="network", retryable=True) from exc
        if "error" in payload:
            raise self._error_from_payload(payload)
        return payload

    @staticmethod
    def _error_from_payload(payload: dict[str, Any], status_code: int | None = None) -> MetaApiError:
        error = payload.get("error", payload)
        code = error.get("code", "")
        subcode = error.get("error_subcode", "")
        retryable = status_code in {429, 500, 502, 503, 504} or int(code or 0) in {1, 2, 4, 17, 32, 613}
        return MetaApiError(
            error.get("message", "Erreur Meta inconnue"),
            code=code,
            subcode=subcode,
            user_message=error.get("error_user_msg", ""),
            retryable=retryable,
            status_code=status_code,
        )

    def _request(self, method: str, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{self.api_root}/{path_or_url.lstrip('/')}"
        complete = dict(params or {})
        if "access_token=" not in url:
            complete.update(self._auth_params())
        transport = self.transport or self._default_transport
        payload = transport(method, url, complete)
        if "error" in payload:
            raise self._error_from_payload(payload)
        return payload

    def _paginate(self, path: str, params: dict[str, Any]) -> list[FetchedPage]:
        pages: list[FetchedPage] = []
        current_path = path
        current_params = dict(params)
        page_number = 1
        while current_path:
            payload = self._request("GET", current_path, current_params)
            safe_payload = self._sanitized_payload(payload)
            pages.append(
                FetchedPage(
                    endpoint=self._sanitized_url(current_path),
                    request_params=self._sanitized_params(current_params),
                    payload=safe_payload,
                    page_number=page_number,
                )
            )
            current_path = payload.get("paging", {}).get("next", "")
            current_params = {}
            page_number += 1
        return pages

    def test_connection(self) -> HealthStatus:
        fields = "id,account_id,name,account_status,currency,timezone_name,timezone_offset_hours_utc"
        payload = self._request("GET", self.account_node, {"fields": fields})
        return HealthStatus(True, "Connexion Meta opérationnelle.", payload)

    def health_status(self) -> HealthStatus:
        try:
            return self.test_connection()
        except MetaApiError as exc:
            return HealthStatus(False, exc.user_message or str(exc), {"code": exc.code, "subcode": exc.subcode})

    def sync_objects(self) -> dict[str, list[FetchedPage]]:
        return {
            object_type: self._paginate(
                f"{self.account_node}/{object_type}",
                {"fields": fields, "limit": 500},
            )
            for object_type, fields in self.OBJECT_FIELDS.items()
        }

    def _insight_params(self, start: date, end: date, level: str) -> dict[str, Any]:
        return {
            "fields": ",".join(self.INSIGHT_FIELDS),
            "level": level,
            "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()}, separators=(",", ":")),
            "time_increment": 1,
            "action_report_time": "impression",
            "use_unified_attribution_setting": "true",
            "limit": 500,
        }

    def _async_insights(self, start: date, end: date, level: str) -> list[FetchedPage]:
        params = self._insight_params(start, end, level)
        started = self._request("POST", f"{self.account_node}/insights", params)
        report_id = started.get("report_run_id")
        if not report_id:
            raise MetaApiError("Meta n’a pas renvoyé d’identifiant de rapport asynchrone.", code="async_missing_id")
        deadline = time.monotonic() + self.async_timeout_seconds
        while time.monotonic() < deadline:
            status = self._request("GET", str(report_id), {"fields": "async_status,async_percent_completion,error_code,error_message"})
            state = status.get("async_status", "")
            if state == "Job Completed":
                pages = self._paginate(f"{report_id}/insights", {"limit": 500})
                return [FetchedPage(p.endpoint, {**p.request_params, "source_report_id": str(report_id), "level": level}, p.payload, p.page_number) for p in pages]
            if state in {"Job Failed", "Job Skipped"}:
                raise MetaApiError(status.get("error_message") or f"Rapport Meta asynchrone: {state}", code=status.get("error_code", "async_failed"))
            time.sleep(self.poll_interval_seconds)
        raise MetaApiError("Le rapport Meta asynchrone a dépassé le délai autorisé.", code="async_timeout", retryable=True)

    def sync_insights(self, start: date, end: date, levels: list[str]) -> dict[str, list[FetchedPage]]:
        use_async = (end - start).days + 1 > 31
        result: dict[str, list[FetchedPage]] = {}
        for level in levels:
            if level not in {"account", "campaign", "adset", "ad"}:
                raise ValueError(f"Unsupported Meta insight level: {level}")
            if use_async:
                result[level] = self._async_insights(start, end, level)
            else:
                result[level] = self._paginate(f"{self.account_node}/insights", self._insight_params(start, end, level))
        return result
