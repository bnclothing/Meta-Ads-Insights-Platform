from .typing import JsonDict
from reporting.models import AuditEvent


def record_audit(*, action: str, user=None, entity=None, metadata: JsonDict | None = None, request=None) -> AuditEvent:
    ip_address = None
    if request is not None:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip_address = (forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")) or None
    return AuditEvent.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        action=action,
        entity_type=entity.__class__.__name__ if entity else "",
        entity_id=str(getattr(entity, "pk", "")) if entity else "",
        metadata=metadata or {},
        ip_address=ip_address,
    )

