"""Best-effort, deterministic diagnostics for individual failed build jobs."""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime

import structlog

from app.utils.github import get_github_actions_client

logger = structlog.get_logger(__name__)

RUN_URL = re.compile(
    r"https://github\.com/(flathub-infra/vorarbeiter)/actions/runs/(\d+)/?$"
)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MODULE_ERROR = re.compile(r"^Error: module:? ([^:\s]+):", re.IGNORECASE)
MODULE_START = re.compile(r"^(?:={3,}\s+)?Building module (\S+) in ")
DIAGNOSTIC = re.compile(
    r"(?:\bfatal error:|\berror(?:\[E\d+\])?:|CMake Error|"
    r"undefined reference|No space left on device|checksum mismatch|^##\[error\])",
    re.IGNORECASE,
)
GENERIC = re.compile(
    r"(?:Process completed with exit code|ninja: build stopped|"
    r"make(?:\[\d+\])?: \*\*\*|"
    r"^Error: module:? [^:\s]+: (?:Child process exited with code|exit code) \d+\.?$|"
    r"error: Recipe .* failed|error: could not compile|"
    r"error: build failed|error: aborting due to)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FailureDiagnostic:
    module: str | None
    excerpt: str | None


def extract_diagnostic(log: str) -> FailureDiagnostic:
    lines = [ANSI.sub("", line).strip() for line in log.splitlines()]
    module = None
    start, end = 0, len(lines)
    for index, line in enumerate(lines):
        if match := MODULE_ERROR.match(line):
            module, end = match[1], index + 1
    # A build-start marker alone is not evidence that that module failed.
    if module:
        start = end - 1
        for index, line in enumerate(lines[:start]):
            if (match := MODULE_START.match(line)) and match[1] == module:
                start = index + 1
    candidates = [
        index
        for index in range(start, end)
        if DIAGNOSTIC.search(lines[index]) and not GENERIC.search(lines[index])
    ]
    if not candidates:
        return FailureDiagnostic(module, None)
    anchor = candidates[0]
    # Reserve the diagnostic before adding context, so a verbose compiler
    # command cannot consume the entire excerpt budget.
    excerpt = lines[anchor]
    if len(excerpt) > 2000:
        excerpt = excerpt[:1999] + "…"
    else:
        for line in lines[anchor + 1 : min(end, anchor + 10)]:
            if GENERIC.search(line):
                continue
            if len(excerpt) + len(line) + 1 > 2000:
                break
            excerpt += "\n" + line
        for line in reversed(lines[max(start, anchor - 2) : anchor]):
            if GENERIC.search(line):
                continue
            if len(excerpt) + len(line) + 1 > 2000:
                break
            excerpt = line + "\n" + excerpt
    return FailureDiagnostic(module, excerpt)


def failed_step_log(log: str, steps: list[dict]) -> str:
    """Only inspect failed steps, never successful probes or later cleanup."""
    ranges = [
        (
            datetime.fromisoformat(step["started_at"]),
            datetime.fromisoformat(step["completed_at"]),
        )
        for step in steps
        if step.get("conclusion") == "failure"
        and step.get("started_at")
        and step.get("completed_at")
    ]
    entries = []
    for line in log.splitlines():
        timestamp, separator, content = line.partition(" ")
        if not separator:
            continue
        try:
            time = datetime.fromisoformat(timestamp)
            # API step timestamps have second precision.
            time = time.replace(microsecond=0)
            entries.append((time, content))
        except ValueError:
            continue

    boundaries = [
        index
        for index, (_, content) in enumerate(entries)
        if content.startswith("##[group]Run ")
    ]
    lines = []
    for start, end in ranges:
        # Second-resolution API timestamps can overlap adjacent steps. Require
        # one unambiguous runner command boundary at the failed step's start.
        starts = [index for index in boundaries if entries[index][0] == start]
        if len(starts) != 1:
            continue
        first = starts[0]
        last = next((index for index in boundaries if index > first), len(entries))
        in_command = True
        for time, content in entries[first + 1 : last]:
            if time > end:
                break
            if in_command:
                if content.startswith("##[endgroup]"):
                    in_command = False
                continue
            if "Process completed with exit code" in content:
                break
            lines.append(content)
    return "\n".join(lines)


def literal(text: str) -> str:
    # Indented code keeps log-controlled fences, HTML and mentions literal.
    return "\n".join("    " + line for line in text.splitlines())


async def build_failure_summary(log_url: str | None) -> str:
    # Enrichment must fit within the workflow callback's request timeout.
    try:
        async with asyncio.timeout(20):
            return await _build_failure_summary(log_url)
    except TimeoutError:
        return "Diagnostic excerpt unavailable; see the full build log."


async def _build_failure_summary(log_url: str | None) -> str:
    fallback = "Diagnostic excerpt unavailable; see the full build log."
    match = RUN_URL.fullmatch(log_url or "")
    if not match:
        return (
            fallback if log_url else "Diagnostic excerpt and build log URL unavailable."
        )
    repo, run_id = match.groups()
    client = get_github_actions_client()
    base = f"https://api.github.com/repos/{repo}/actions"
    try:
        # This workflow has a small fixed job graph. Request up to 100 jobs.
        response = await client.request(
            "get", f"{base}/runs/{run_id}/jobs?per_page=100", max_retries=0
        )
        if response is None:
            return fallback
        jobs = [
            job
            for job in response.json().get("jobs", [])
            if job.get("conclusion") == "failure"
            and (
                job.get("name", "").startswith("build-")
                or job.get("name") == "validate-manifest"
            )
        ]
        sections = []
        for job in sorted(jobs, key=lambda job: job["id"])[:4]:
            job_id = int(job["id"])
            url = f"https://github.com/{repo}/actions/runs/{run_id}/job/{job_id}"
            section = (
                f"**Failed job:**\n\n{literal(job['name'])}\n\n[Full job log]({url})"
            )
            try:
                response = await client.request(
                    "get",
                    f"{base}/jobs/{job_id}/logs",
                    follow_redirects=True,
                    max_retries=0,
                )
                diagnostic = (
                    extract_diagnostic(
                        failed_step_log(response.text, job.get("steps", []))
                    )
                    if response is not None
                    else FailureDiagnostic(None, None)
                )
                if diagnostic.module:
                    section += f"\n\n**Failed module:**\n\n{literal(diagnostic.module)}"
                else:
                    section += "\n\nModule not identified."
                if diagnostic.excerpt:
                    section += (
                        f"\n\n**Diagnostic excerpt:**\n\n{literal(diagnostic.excerpt)}"
                    )
                else:
                    section += "\n\nNo specific diagnostic could be extracted; see the full job log."
            except Exception:
                logger.exception("Failed to extract job diagnostic", job_id=job_id)
                section += f"\n\n{fallback}"
            sections.append(section)
        return "\n\n".join(sections) or fallback
    except Exception:
        logger.exception("Failed to retrieve build diagnostics", run_id=run_id)
        return fallback
