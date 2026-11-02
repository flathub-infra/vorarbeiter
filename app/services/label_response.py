import hashlib

import structlog
from sqlalchemy import text

from app.database import get_db
from app.utils.github import ensure_pr_comment

logger = structlog.get_logger(__name__)

TOO_NEW_RESPONSE = (
    "Flathub requires a meaningful history of development or existence, evidence of "
    "real-world use, and a clear commitment to ongoing maintenance. Please continue "
    "developing the application or share any evidence of earlier development or use. "
    "Otherwise, your submission will be revisited in 1-2 months.\n"
    "\n"
    "See the [development history requirements]"
    "(https://docs.flathub.org/docs/for-app-authors/requirements"
    "#insufficient-development-history)."
)

LABEL_RESPONSES = {"too-new": TOO_NEW_RESPONSE}

MARKER_TEMPLATE = "<!-- vorarbeiter:label-response:{label} -->"


async def respond_to_label(git_repo: str, pr_number: int, label: str) -> bool:
    response = LABEL_RESPONSES.get(label)
    if response is None:
        return False

    marker = MARKER_TEMPLATE.format(label=label)
    body = f"{response}\n\n{marker}"
    try:
        async with get_db() as db:
            if db.get_bind().dialect.name != "sqlite":
                key = int.from_bytes(
                    hashlib.sha256(
                        f"label-response:{git_repo}:{pr_number}:{label}".encode()
                    ).digest()[:8],
                    "big",
                    signed=True,
                )
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
                )
            posted = await ensure_pr_comment(git_repo, pr_number, marker, body)
    except Exception:
        logger.exception(
            "Failed to post label response",
            git_repo=git_repo,
            pr_number=pr_number,
            label=label,
        )
        return False

    if not posted:
        logger.warning(
            "Could not check or post label response comment",
            git_repo=git_repo,
            pr_number=pr_number,
            label=label,
        )
    return posted
