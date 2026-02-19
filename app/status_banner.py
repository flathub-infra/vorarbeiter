import time
import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

_CACHE_TTL = 7200

BannerData = dict[str, str | list[dict[str, str]]]


class StatusBannerCache:
    def __init__(self) -> None:
        self.data: BannerData | None = None
        self.timestamp: float = -_CACHE_TTL

    def is_valid(self, now: float) -> bool:
        return now - self.timestamp < _CACHE_TTL

    def set(self, data: BannerData | None, now: float) -> None:
        self.data = data
        self.timestamp = now


_banner_cache = StatusBannerCache()

_SEVERITY = {
    "ok": None,
    "disrupted": "error",
    "down": "error",
    "notice": "info",
}

_LABELS = {
    "warn": "Degraded",
    "error": "Outage",
    "info": "Notice",
}


async def get_status_banner(
    _cache: StatusBannerCache | None = None,
) -> BannerData | None:
    cache = _cache if _cache is not None else _banner_cache
    now = time.monotonic()

    if cache.is_valid(now):
        age = int(now - cache.timestamp)
        expires_in = int(_CACHE_TTL - age)
        logger.info(
            "Returning cached statuspage feed",
            summary_status=cache.data.get("summary_status") if cache.data else None,
            cache_age_seconds=age,
            cache_expires_in_seconds=expires_in,
            cache_ttl_seconds=_CACHE_TTL,
        )
        return cache.data

    logger.info("Fetching fresh statuspage feed")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.statuspage_url}/index.json")
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning(
            "Failed to fetch fresh statuspage feed, using cached data",
            error=str(e),
            cache_age_seconds=int(now - cache.timestamp) if cache.timestamp else None,
        )
        return cache.data

    summary = data.get("summaryStatus", "ok")
    if summary not in ("disrupted", "down"):
        logger.info("Statuspage feed reports no active outage")
        cache.set(None, now)
        return None

    severity = _SEVERITY.get(summary)
    if not severity:
        cache.set(None, now)
        return None

    seen: set[str] = set()
    issues: list[dict[str, str]] = []
    for system in data.get("systems", []):
        for issue in system.get("unresolvedIssues", []):
            if issue.get("resolved", False):
                logger.debug("Ignoring resolved issue in statuspage feed")
                continue
            if issue.get("informational", False):
                logger.debug("Ignoring informational issue in statuspage feed")
                continue
            permalink = issue.get("permalink", "")
            if permalink in seen:
                continue
            seen.add(permalink)
            affected = issue.get("affected", [])
            issues.append(
                {
                    "system": ", ".join(affected) if affected else system["name"],
                    "title": issue.get("title", ""),
                    "permalink": permalink,
                    "severity": issue.get("severity", summary),
                }
            )

    result: BannerData = {
        "severity": severity,
        "label": _LABELS[severity],
        "summary_status": summary,
        "issues": issues,
        "status_url": settings.statuspage_url,
    }
    cache.set(result, now)
    return result
