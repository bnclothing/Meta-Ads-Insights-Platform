$ErrorActionPreference = "Stop"

$projectDirectory = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$startingDirectory = Get-Location

$diagnosticCode = @'
import json
from django.db.models import Count, Max, Min
from reporting.models import (
    AdAccount,
    AdSetSnapshot,
    AdSnapshot,
    CampaignSnapshot,
    InsightDaily,
    MetaConnection,
    SyncRun,
)

connection = MetaConnection.objects.order_by("id").first()
account = AdAccount.objects.order_by("id").first()
insight_summary = InsightDaily.objects.aggregate(
    total=Count("id"),
    first_date=Min("date"),
    last_date=Max("date"),
)
insights_by_level = {
    item["level"]: item["count"]
    for item in InsightDaily.objects.values("level").annotate(count=Count("id")).order_by("level")
}

runs = []
for run in SyncRun.objects.prefetch_related("errors", "raw_payloads").order_by("-id")[:10]:
    pages = []
    for page in run.raw_payloads.all().order_by("id"):
        payload = page.payload if isinstance(page.payload, dict) else {}
        data = payload.get("data")
        pages.append(
            {
                "endpoint": page.endpoint,
                "page": page.page_number,
                "rows": len(data) if isinstance(data, list) else None,
                "payload_keys": sorted(payload.keys()),
            }
        )
    runs.append(
        {
            "id": run.id,
            "range": f"{run.requested_start}..{run.requested_end}",
            "status": run.status,
            "message": run.message,
            "records": run.records_count,
            "raw_pages": run.raw_pages_count,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "errors": [
                {
                    "level": error.level,
                    "code": error.code,
                    "subcode": error.subcode,
                    "message": error.user_message or error.message,
                }
                for error in run.errors.all()
            ],
            "pages": pages,
        }
    )

result = {
    "connection": {
        "configured": bool(connection),
        "status": connection.status if connection else None,
        "last_error": connection.last_error if connection else None,
    },
    "account": {
        "id": account.external_id if account else None,
        "name": account.name if account else None,
        "timezone": account.timezone_name if account else None,
        "currency": account.currency if account else None,
    },
    "objects": {
        "campaigns": CampaignSnapshot.objects.count(),
        "adsets": AdSetSnapshot.objects.count(),
        "ads": AdSnapshot.objects.count(),
    },
    "insights": {
        **insight_summary,
        "by_level": insights_by_level,
    },
    "sync_runs": runs,
}
print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
'@

try {
    Set-Location -LiteralPath $projectDirectory
    $diagnosticCode | docker compose exec -T web python manage.py shell
    if ($LASTEXITCODE -ne 0) {
        throw "The diagnostic command could not read the application database."
    }
}
finally {
    Set-Location -LiteralPath $startingDirectory
}
