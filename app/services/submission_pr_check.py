import re
import time
from dataclasses import dataclass, field
from publicsuffixlist import PublicSuffixList
from typing import Any

import structlog

from app.utils.github import (
    create_pr_comment,
    get_github_client,
    get_pr_files,
    get_pr_bot_comments,
    get_pr_unresolved_review_thread_count,
    update_pr_label,
)

logger = structlog.get_logger(__name__)

_BUILD_SUCCESS_COMMENT = "[Test build succeeded]"
_BUILD_START_COMMENT_PARTIAL = "Starting a test build of the submission"
_REVIEW_COMMENT_PARTIAL = "This pull request is temporarily marked as blocked as some"

PSL = PublicSuffixList()

_PR_TEMPLATE_CACHE: list[str] | None = None
_PR_TEMPLATE_CACHE_TS: float = 0.0
_PR_TEMPLATE_CACHE_TTL = 3600.0


def get_domain(appid: str) -> str | None:
    logins = (
        "com.github.",
        "com.gitlab.",
        "io.github.",
        "io.gitlab.",
        "org.gnome.gitlab.",
        "org.gnome.World.",
        "org.gnome.design",
        "org.kde.",
        "org.gnome.",
    )
    platform_prefixes = (
        "org.freedesktop.Platform.",
        "org.freedesktop.Sdk.",
        "org.gnome.Platform.",
        "org.gnome.Sdk.",
        "org.gtk.Gtk3theme.",
        "org.kde.KStyle.",
        "org.kde.Platform.",
        "org.kde.PlatformInputContexts.",
        "org.kde.PlatformTheme.",
        "org.kde.Sdk.",
        "org.kde.WaylandDecoration.",
        "org.freedesktop.LinuxAudio.",
    )
    forge_prefixes = (
        "io.frama.",
        "page.codeberg.",
        "io.sourceforge.",
        "net.sourceforge.",
    )
    extension_keywords = (
        "addon",
        "addons",
        "extension",
        "extensions",
        "plugin",
        "plugins",
    )

    def demangle(name: str) -> str:
        if name[:1] == "_" and name[1:2].isdigit():
            name = name[1:]
        return name.replace("_", "-")

    parts = appid.split(".")

    if (
        appid.count(".") < 2
        or appid.startswith(logins + platform_prefixes)
        or appid.endswith(".BaseApp")
        or parts[-2].lower() in extension_keywords
    ):
        return None

    if appid.startswith(forge_prefixes):
        tld, domain, name = parts[:3]
        name = demangle(name)
        if domain == "sourceforge":
            return f"{name}.{domain}.io".lower()
        return f"{name}.{domain}.{tld}".lower()

    fqdn = ".".join(reversed([demangle(i) for i in parts])).lower()
    if PSL.is_private(fqdn):
        short_domain = PSL.privatesuffix(fqdn)
        if short_domain:
            return short_domain

    return ".".join(reversed([demangle(p) for p in parts[:-1]])).lower()


def validate_title(title: str) -> bool:
    m = re.match(r"(?i)^add\s+", title)
    if not m:
        return False
    appid = title[m.end() :].strip()
    parts = appid.split(".")
    return 3 <= len(parts) <= 255 and all(
        re.match(r"^[A-Za-z_][\w\-]*$", p) for p in parts
    )


def appid_from_title(title: str) -> str | None:
    m = re.match(r"(?i)^add\s+", title)
    if not m:
        return None
    return title[m.end() :].strip() or None


@dataclass
class PRCheckResult:
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    domain: str | None = None
    appid: str | None = None
    is_draft: bool = False


class PRCheckService:
    git_repo = "flathub/flathub"

    _PR_TEMPLATE_URL = "https://raw.githubusercontent.com/flathub/flathub/refs/heads/master/.github/pull_request_template.md"

    _BUILD_START_COMMENT = """\
Starting a test build of the submission. Please fix any \
issues reported in the build log. You can restart the build \
once the issue is fixed by commenting the phrase below.

bot, build"""

    _BASE_REVIEW_COMMENT = """\
This pull request is temporarily marked as blocked as some \
automated checks failed on it. Please make sure the \
following items are done:"""

    async def handle_pr_event(self, payload: dict[str, Any]) -> None:
        action = payload.get("action")
        pr = payload.get("pull_request", {})
        pr_number: int = pr.get("number")
        base_ref: str = pr.get("base", {}).get("ref", "")

        if base_ref != "new-pr":
            logger.debug(
                "Ignoring PR event: not targeting new-pr branch",
                pr_number=pr_number,
                base_ref=base_ref,
            )
            return

        if action not in (
            "opened",
            "synchronize",
            "reopened",
            "ready_for_review",
            "edited",
        ):
            logger.debug(
                "Ignoring PR event: unhandled action",
                pr_number=pr_number,
                action=action,
            )
            return

        logger.info("Handling PR check event", pr_number=pr_number, action=action)

        labels = {lbl["name"] for lbl in pr.get("labels", [])}

        if labels & {"Stale", "stale-blocked"}:
            logger.debug(
                "Ignoring PR event: PR is stale",
                pr_number=pr_number,
            )
            return

        is_draft = pr.get("draft", False)
        title = pr.get("title", "")
        body = pr.get("body") or ""
        files = await get_pr_files(self.git_repo, pr_number)
        existing_comments = await get_pr_bot_comments(self.git_repo, pr_number)

        result = await self._validate(title, body, files, is_draft)
        await self._apply_labels_and_comments(
            pr_number, result, labels, existing_comments
        )

        if not result.is_draft and not result.blocked:
            existing_comments = await get_pr_bot_comments(self.git_repo, pr_number)
            await self._sync_review_thread_labels(pr_number, labels, existing_comments)

    async def handle_pull_request_review_event(self, payload: dict[str, Any]) -> None:
        pr = payload.get("pull_request", {})
        pr_number: int = pr.get("number")
        base_ref: str = pr.get("base", {}).get("ref", "")

        if base_ref != "new-pr":
            return

        if pr.get("draft", False):
            return

        labels = {lbl["name"] for lbl in pr.get("labels", [])}

        if labels & {"Stale", "stale-blocked"}:
            return

        existing_comments = await get_pr_bot_comments(self.git_repo, pr_number)
        await self._sync_review_thread_labels(pr_number, labels, existing_comments)

    @staticmethod
    def _latest_build_succeeded(comments: str) -> bool:
        build_lines = [line for line in comments.splitlines() if "Test build" in line]
        return bool(build_lines) and _BUILD_SUCCESS_COMMENT in build_lines[-1]

    async def _sync_review_thread_labels(
        self, pr_number: int, labels: set[str], existing_comments: str
    ) -> None:
        unresolved = await get_pr_unresolved_review_thread_count(
            self.git_repo, pr_number
        )
        if unresolved is None:
            return

        if unresolved > 0 and "awaiting-review" in labels:
            await update_pr_label(
                self.git_repo, pr_number, "awaiting-review", add=False
            )
            await update_pr_label(
                self.git_repo, pr_number, "awaiting-changes", add=True
            )
        elif (
            unresolved == 0
            and "awaiting-changes" in labels
            and not labels
            & {"awaiting-upstream", "work-in-progress", "pr-check-blocked", "blocked"}
            and self._latest_build_succeeded(existing_comments)
        ):
            await update_pr_label(
                self.git_repo, pr_number, "awaiting-changes", add=False
            )
            await update_pr_label(self.git_repo, pr_number, "awaiting-review", add=True)

    async def _validate(
        self,
        title: str,
        body: str,
        files: list[str],
        is_draft: bool,
    ) -> PRCheckResult:
        result = PRCheckResult(appid=appid_from_title(title), is_draft=is_draft)

        if is_draft:
            return result

        if not validate_title(title):
            result.blocked = True
            result.reasons.append('- PR title is "Add $FLATPAK_ID"')

        if not any(re.match(r"^[^/]+\.(ya?ml|json)$", f) for f in files):
            result.blocked = True
            result.reasons.append("- Flatpak manifest is at toplevel")

        if any(re.match(r".+/flathub\.json$", f) for f in files):
            result.blocked = True
            result.reasons.append("- flathub.json file is at toplevel")

        checklist_ok, checklist_reason = await self._check_checklist(body)
        if not checklist_ok:
            result.blocked = True
            result.reasons.append(checklist_reason)

        if result.blocked:
            result.reasons.append(
                "- The [requirements](https://docs.flathub.org/docs/for-app-authors/requirements) "
                "and [submission process](https://docs.flathub.org/docs/for-app-authors/submission) "
                "have been followed"
            )
        elif result.appid:
            result.domain = get_domain(result.appid)

        return result

    @classmethod
    async def _fetch_checklist_items(cls) -> list[str]:
        global _PR_TEMPLATE_CACHE, _PR_TEMPLATE_CACHE_TS

        now = time.monotonic()
        if (
            _PR_TEMPLATE_CACHE is not None
            and now - _PR_TEMPLATE_CACHE_TS < _PR_TEMPLATE_CACHE_TTL
        ):
            return _PR_TEMPLATE_CACHE

        client = get_github_client()
        response = await client.request("get", cls._PR_TEMPLATE_URL)
        if not response:
            logger.warning(
                "Failed to fetch PR template, falling back to empty checklist"
            )
            return []

        _PR_TEMPLATE_CACHE = [
            line.removeprefix("- [ ]").strip()
            for line in response.text.splitlines()
            if line.startswith("- [ ]")
        ]
        _PR_TEMPLATE_CACHE_TS = now
        return _PR_TEMPLATE_CACHE

    @staticmethod
    def _is_item_addressed(item: str, lines: list[str]) -> bool:
        return any(
            (re.search(r"\[[xX]\]", line) or "N/A" in line) and item in line
            for line in lines
        )

    async def _check_checklist(self, body: str) -> tuple[bool, str]:
        reason = (
            "- All [checklists](https://github.com/flathub/flathub/blob/master/"
            ".github/pull_request_template.md?plain=1) are present in PR body "
            "and are completed"
        )
        lines = body.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        template_items = await self._fetch_checklist_items()
        if not template_items:
            return False, reason
        if not all(self._is_item_addressed(item, lines) for item in template_items):
            return False, reason
        return True, ""

    async def _apply_labels_and_comments(
        self,
        pr_number: int,
        result: PRCheckResult,
        labels: set[str],
        existing_comments: str,
    ) -> None:
        if result.is_draft:
            await update_pr_label(
                self.git_repo, pr_number, "work-in-progress", add=True
            )
            return

        if "work-in-progress" in labels:
            await update_pr_label(
                self.git_repo, pr_number, "work-in-progress", add=False
            )

        if result.blocked:
            await update_pr_label(
                self.git_repo, pr_number, "pr-check-blocked", add=True
            )
            if "awaiting-review" in labels:
                await update_pr_label(
                    self.git_repo, pr_number, "awaiting-review", add=False
                )
            if _REVIEW_COMMENT_PARTIAL not in existing_comments:
                comment = self._BASE_REVIEW_COMMENT + "".join(
                    f"\n{reason}" for reason in result.reasons
                )
                await create_pr_comment(
                    git_repo=self.git_repo, pr_number=pr_number, comment=comment
                )
        else:
            if "pr-check-blocked" in labels:
                await update_pr_label(
                    self.git_repo, pr_number, "pr-check-blocked", add=False
                )
            if not labels & {
                "awaiting-changes",
                "awaiting-upstream",
                "blocked",
                "reviewed-waiting",
            }:
                await update_pr_label(
                    self.git_repo, pr_number, "awaiting-review", add=True
                )

            if result.domain and "blocked" not in labels:
                verif_url = (
                    f"https://{result.domain}/.well-known/org.flathub.VerifiedApps.txt"
                )
                if verif_url not in existing_comments:
                    comment = (
                        f"The domain to be used for verification is {result.domain}. "
                        f"If you intend to [verify](https://docs.flathub.org/docs/for-app-authors/verification) "
                        f"this submission, please confirm by uploading an empty "
                        f"`org.flathub.VerifiedApps.txt` file to {verif_url}. "
                        f"Otherwise, ignore this. Please comment if this is incorrect."
                    )
                    await create_pr_comment(
                        git_repo=self.git_repo, pr_number=pr_number, comment=comment
                    )

            if (
                _BUILD_START_COMMENT_PARTIAL not in existing_comments
                and not self._latest_build_succeeded(existing_comments)
                and not labels & {"pr-check-blocked", "blocked"}
            ):
                await create_pr_comment(
                    git_repo=self.git_repo,
                    pr_number=pr_number,
                    comment=self._BUILD_START_COMMENT,
                )
