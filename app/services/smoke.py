"""Bind application-check results to builds without changing publication status."""

import base64
import hashlib
import html
import json
import re
import zipfile
from urllib.parse import quote

import structlog
from sqlalchemy import select, text

from app.config import settings
from app.database import get_db
from app.models import Pipeline, SmokeResult
from app.services.smoke_artifacts import API, download_artifact, run_artifacts
from app.smoke import smoke_config
from app.utils.github import get_github_actions_client, get_github_client

logger = structlog.get_logger(__name__)
TITLE = re.compile(r"smoke build ([1-9][0-9]*) attempt ([1-9][0-9]*)")
MARKER = re.compile(r"<!-- vorarbeiter-smoke:(\d+):(\d+):(\d+):(\d+) -->")


async def _json(client, method, url, **kwargs):
    response = await client.request(method, url, **kwargs)
    if response is None:
        raise RuntimeError("GitHub request failed while reporting smoke tests")
    return response.json()


async def _lock(db, key: str):
    # PostgreSQL serializes different workers too. SQLite tests run in one process.
    if db.get_bind().dialect.name == "postgresql":
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], signed=True)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


def _escape(value: str) -> str:
    value = html.escape(value).replace("@", "@\u200b")
    return re.sub(r"([\\`*_[\]()])", r"\\\1", value)


def comment_body(pipeline: Pipeline, result: SmokeResult, source_run: int) -> str:
    summary = result.summary
    body = (
        f"<!-- vorarbeiter-smoke:{source_run}:{result.source_attempt}:{result.run_id}:{result.run_attempt} -->\n"
        "### Application startup and screenshot checks\n\n"
        f"Commit `{pipeline.params.get('sha', '')}` · {_escape(summary.get('arch', 'x86_64'))}\n\n"
        f"- Startup verification: **{summary['verify'].replace('_', ' ')}**\n"
        f"- Screenshot capture: **{summary['screenshots'].replace('_', ' ')}**\n"
    )
    for message in summary.get("messages", [])[:4]:
        body += f"\n{_escape(str(message)[:1000])}\n"
    run_url = (
        f"https://github.com/flathub-infra/vorarbeiter/actions/runs/{result.run_id}"
    )
    body += f"\n[Workflow run]({run_url})"
    if result.artifact_id:
        body += f" · [Download screenshots and logs]({run_url}/artifacts/{result.artifact_id})"
        for preview in summary.get("previews", [])[:3]:
            url = f"{settings.base_url.rstrip('/')}/smoke/{pipeline.id}/{result.artifact_id}/{quote(preview['path'], safe='/')}"
            body += f"\n\n![{_escape(preview['caption'])}]({url})"
    return (
        body
        + "\n\nThese checks do not block publication. Artifacts and previews are retained for 14 days.\n"
    )


class SmokeService:
    async def process_run(self, run_id: int) -> bool:
        """Process only infrastructure-owned smoke runs with a known source build."""
        client = get_github_actions_client()
        run = await _json(client, "get", f"{API}/actions/runs/{run_id}")
        match = TITLE.fullmatch(run.get("display_title", ""))
        if (
            not match
            or run.get("path") != ".github/workflows/smoke.yml"
            or run.get("event") != "workflow_run"
            or run.get("status") != "completed"
        ):
            return False
        source_run, source_attempt = map(int, match.groups())
        attempt = int(run["run_attempt"])
        parent = await _json(
            client, "get", f"{API}/actions/runs/{source_run}/attempts/{source_attempt}"
        )
        if (
            parent.get("path") != ".github/workflows/build.yml"
            or parent.get("event") != "workflow_dispatch"
        ):
            return False
        if parent.get("conclusion") != "success":
            jobs = await _json(
                client,
                "get",
                f"{API}/actions/runs/{source_run}/attempts/{source_attempt}/jobs",
                params={"per_page": 100},
            )
            required = {"Build", "Validate build", "Upload build"}
            if not any(
                job.get("name") in {"build-x86_64", "build-aarch64"}
                and required
                <= {
                    step["name"]
                    for step in job.get("steps", [])
                    if step.get("conclusion") == "success"
                }
                for job in jobs.get("jobs", [])
            ):
                return False
        async with get_db() as db:
            pipeline = (
                (
                    await db.execute(
                        select(Pipeline).where(
                            Pipeline.log_url
                            == f"https://github.com/flathub-infra/vorarbeiter/actions/runs/{source_run}"
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if pipeline is None:
                raise RuntimeError("source build pipeline is not available yet")
            await _lock(db, f"smoke-pipeline:{pipeline.id}")
            result = await db.get(SmokeResult, pipeline.id)
            if (
                result
                and (result.source_attempt, result.run_id, result.run_attempt)
                >= (source_attempt, run_id, attempt)
                and result.notified
            ):
                return False

            inputs = await run_artifacts(source_run)
            selected = next(
                (
                    a
                    for a in inputs
                    if a["name"] == f"smoke-input-{source_attempt}-x86_64"
                ),
                None,
            )
            selected = selected or next(
                (
                    a
                    for a in inputs
                    if a["name"] == f"smoke-input-{source_attempt}-aarch64"
                ),
                None,
            )
            summary = {
                "verify": "infrastructure_error",
                "screenshots": "skipped",
                "messages": [],
                "previews": [],
                "arch": "x86_64",
            }
            enabled = True
            if selected is None:
                enabled = await self._configured(pipeline)
                summary["messages"] = [
                    "Smoke inputs were not uploaded for this build attempt"
                ]
            artifact_id = None
            if enabled and selected:
                artifacts = await run_artifacts(run_id)
                artifact = next(
                    (a for a in artifacts if a["name"] == f"smoke-result-{attempt}"),
                    None,
                )
                if artifact:
                    try:
                        parsed = await download_artifact(artifact)
                        expected_sha = pipeline.params.get("sha")
                        if (
                            parsed.source_sha is not None
                            and expected_sha
                            and parsed.source_sha != expected_sha
                        ):
                            raise ValueError(
                                "tested source revision differs from the requested build revision"
                            )
                        if (
                            parsed.app_ref is not None
                            and parsed.app_ref.split("/")[1] != pipeline.app_id
                        ):
                            raise ValueError(
                                "tested app ref differs from the requested application"
                            )
                        summary = parsed.summary
                        artifact_id = int(artifact["id"])
                    except (
                        ValueError,
                        KeyError,
                        TypeError,
                        zipfile.BadZipFile,
                        FileNotFoundError,
                    ) as error:
                        summary["messages"] = [
                            f"Could not read smoke results: {str(error)[:500]}"
                        ]
                else:
                    summary["messages"] = [
                        "The smoke workflow did not upload a result artifact"
                    ]
            if run.get("conclusion") == "cancelled" and enabled:
                summary["verify"] = "cancelled"
            if result is None:
                result = SmokeResult(pipeline_id=pipeline.id)
                db.add(result)
            result.run_id, result.run_attempt, result.source_attempt = (
                run_id,
                attempt,
                source_attempt,
            )
            result.artifact_id, result.summary, result.notified = (
                artifact_id,
                summary,
                False,
            )
            if enabled:
                await self._notify(db, pipeline, result, source_run)
            result.notified = True
            return True

    async def _configured(self, pipeline: Pipeline) -> bool:
        """Distinguish disabled apps from missing inputs after preparation/upload failure."""
        repo, sha = pipeline.params.get("repo"), pipeline.params.get("sha")
        if not repo or not sha:
            return False
        response = await get_github_client().request(
            "get",
            f"https://api.github.com/repos/{repo}/contents/flathub.json",
            params={"ref": sha},
            raise_for_status=False,
        )
        if response is None:
            raise RuntimeError("could not check the source revision's smoke opt-in")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        try:
            return (
                smoke_config(json.loads(base64.b64decode(response.json()["content"])))
                is not None
            )
        except (ValueError, KeyError, TypeError):
            return True

    async def _notify(
        self, db, pipeline: Pipeline, result: SmokeResult, source_run: int
    ):
        repo, number = pipeline.params.get("repo"), pipeline.params.get("pr_number")
        if not repo or not number:
            return
        await _lock(db, f"smoke-pr:{repo}:{number}")
        client = get_github_client()
        api = f"https://api.github.com/repos/{repo}"
        pr = await _json(client, "get", f"{api}/pulls/{int(number)}")
        if pr.get("state") != "open" or pr.get("head", {}).get(
            "sha"
        ) != pipeline.params.get("sha"):
            return
        bot = await _json(client, "get", "https://api.github.com/user")
        comment = None
        for page in range(1, 101):
            comments = await _json(
                client,
                "get",
                f"{api}/issues/{int(number)}/comments",
                params={"per_page": 100, "page": page},
            )
            for candidate in comments:
                marker = MARKER.search(candidate.get("body", ""))
                if candidate.get("user", {}).get("id") == bot["id"] and marker:
                    order = tuple(map(int, marker.groups()))
                    if order >= (
                        source_run,
                        result.source_attempt,
                        result.run_id,
                        result.run_attempt,
                    ):
                        return
                    comment = candidate
            if len(comments) < 100:
                break
        else:
            raise RuntimeError(
                "too many PR comments to locate application-check report"
            )
        body = json.dumps({"body": comment_body(pipeline, result, source_run)})
        if comment:
            await _json(
                client,
                "patch",
                f"{api}/issues/comments/{int(comment['id'])}",
                content=body,
            )
        else:
            await _json(
                client,
                "post",
                f"{api}/issues/{int(number)}/comments",
                content=body,
                max_retries=0,
            )

    async def reconcile(self) -> int:
        """Recover missed deliveries, including reruns of older workflows."""
        client = get_github_actions_client()
        processed = 0
        async with get_db() as db:
            records = (await db.execute(select(SmokeResult))).scalars().all()
            done = {(r.run_id, r.run_attempt) for r in records if r.notified}
        for page in range(1, 11):
            listing = await _json(
                client,
                "get",
                f"{API}/actions/workflows/smoke.yml/runs",
                params={
                    "status": "completed",
                    "per_page": 100,
                    "page": page,
                },
            )
            runs = listing["workflow_runs"]
            for run in runs:
                if (run["id"], run["run_attempt"]) in done:
                    continue
                try:
                    processed += int(await self.process_run(run["id"]))
                except Exception:
                    logger.exception(
                        "Could not reconcile smoke result", run_id=run["id"]
                    )
            if len(runs) < 100:
                break
        return processed
