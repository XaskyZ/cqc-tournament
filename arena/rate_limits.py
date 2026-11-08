"""Database-backed rate limits shared by separate serverless invocations."""
import ipaddress
import math
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import RateLimitBucket


def client_address(request):
    # Enable only when the entire origin is served behind Vercel's trusted edge.
    # Arbitrary X-Forwarded-For from a direct client must not bypass a counter.
    if getattr(settings, "RATE_LIMIT_TRUST_VERCEL_IP", False):
        raw = request.META.get("HTTP_X_VERCEL_FORWARDED_FOR", "")
    else:
        raw = request.META.get("REMOTE_ADDR", "")
    try:
        address = ipaddress.ip_address(raw.strip())
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address)
    except (ValueError, AttributeError):
        # Malformed/missing addresses share a bucket instead of escaping limits.
        return "unknown"


def consume(address, scope, limit, seconds):
    """Return (allowed, retry_after). Never fall back to a per-process counter."""
    key = salted_hmac("arena.rate-limit", f"{scope}:{address}", algorithm="sha256").hexdigest()
    with transaction.atomic():
        now = timezone.now()
        # Unique PK plus get_or_create's savepoint handles first-hit races.
        # A conflicting creator is subsequently read with FOR UPDATE as well.
        bucket, _ = RateLimitBucket.objects.select_for_update().get_or_create(
            key=key, defaults={"expires_at": now + timedelta(seconds=seconds)},
        )
        # Compute time after acquiring the lock, which may have waited.
        now = timezone.now()
        if bucket.expires_at <= now:
            bucket.count = 0
            bucket.expires_at = now + timedelta(seconds=seconds)
        bucket.count = min(bucket.count + 1, limit + 1)
        bucket.save(update_fields=["count", "expires_at"])
        return bucket.count <= limit, max(1, math.ceil((bucket.expires_at - now).total_seconds()))


def purge_expired(*, batch_size=1000):
    """Bounded maintenance, safe against a bucket concurrently being renewed."""
    now = timezone.now()
    keys = list(RateLimitBucket.objects.filter(expires_at__lte=now).order_by("expires_at").values_list("pk", flat=True)[:batch_size])
    deleted, _ = RateLimitBucket.objects.filter(pk__in=keys, expires_at__lte=now).delete()
    return deleted
