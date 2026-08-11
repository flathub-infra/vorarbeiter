import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import BuildFailureIssue, Pipeline, PipelineStatus
from app.services.build_failure_issue import BuildFailureIssueService

SHA_A = "a" * 40
SHA_B = "b" * 40
LOG_A = "https://github.com/flathub-infra/vorarbeiter/actions/runs/100"
LOG_B = "https://github.com/flathub-infra/vorarbeiter/actions/runs/200"


@pytest.fixture
def service_database(db_session_maker):
    @asynccontextmanager
    async def get_test_db():
        async with db_session_maker() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    with patch("app.services.build_failure_issue.get_db", get_test_db):
        yield db_session_maker


def make_pipeline(
    *,
    sha: str = SHA_A,
    log_url: str | None = LOG_A,
    repo: str = "flathub/org.test.App",
    flat_manager_repo: str = "stable",
    retry_from_issue: int | None = None,
) -> Pipeline:
    params = {"sha": sha, "repo": repo}
    if retry_from_issue is not None:
        params["retry_from_issue"] = retry_from_issue
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.FAILED,
        params=params,
        flat_manager_repo=flat_manager_repo,
        log_url=log_url,
    )


async def get_state(db_session_maker) -> BuildFailureIssue | None:
    async with db_session_maker() as db:
        return (
            await db.execute(
                select(BuildFailureIssue).where(
                    BuildFailureIssue.app_id == "org.test.App"
                )
            )
        ).scalar_one_or_none()


async def add_state(
    db_session_maker,
    *,
    issue_number: int | None = 10,
    sha: str | None = SHA_A,
    log_url: str | None = LOG_A,
) -> None:
    async with db_session_maker() as db:
        db.add(
            BuildFailureIssue(
                app_id="org.test.App",
                git_repo="flathub/org.test.App",
                issue_number=issue_number,
                latest_sha=sha,
                latest_build_url=log_url,
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_first_failure_creates_and_tracks_issue(service_database):
    pipeline = make_pipeline()
    with (
        patch(
            "app.services.build_failure_issue.list_open_github_issues",
            AsyncMock(return_value=[]),
        ) as list_issues,
        patch(
            "app.services.build_failure_issue.create_github_issue",
            AsyncMock(return_value=("https://github.com/issues/10", 10)),
        ) as create_issue,
    ):
        await BuildFailureIssueService().handle_result(pipeline, "failure")

    state = await get_state(service_database)
    assert state is not None
    assert state.git_repo == "flathub/org.test.App"
    assert state.issue_number == 10
    assert state.latest_sha == SHA_A
    assert state.latest_build_url == LOG_A
    list_issues.assert_awaited_once_with("flathub/org.test.App")
    create_issue.assert_awaited_once_with(
        git_repo="flathub/org.test.App",
        title="Stable build failed",
        body=(
            "The stable build pipeline for `org.test.App` failed.\n\n"
            f"Commit SHA: {SHA_A}\nBuild log: {LOG_A}\n\n"
            "Please check the logs for details. If the failure was unexpected, "
            "you can retry the build by commenting `bot, retry` in this issue."
            "\n\ncc @flathub/build-moderation"
        ),
    )


@pytest.mark.asyncio
async def test_first_result_adopts_highest_matching_legacy_issue(service_database):
    expected_prefix = "The stable build pipeline for `org.test.App` failed."
    issues = [
        {"number": 2, "title": "Stable build failed", "body": expected_prefix},
        {"number": 8, "title": "Stable build failed", "body": expected_prefix},
        {
            "number": 12,
            "title": "Stable build failed",
            "body": expected_prefix,
            "pull_request": {},
        },
        {"number": 20, "title": "Different", "body": expected_prefix},
        {
            "number": 21,
            "title": "Stable build failed",
            "body": "A user-authored issue",
        },
    ]
    pipeline = make_pipeline(sha=SHA_B, log_url=LOG_B)
    with (
        patch(
            "app.services.build_failure_issue.list_open_github_issues",
            AsyncMock(return_value=issues),
        ),
        patch(
            "app.services.build_failure_issue.get_github_issue",
            AsyncMock(return_value={"state": "open"}),
        ),
        patch(
            "app.services.build_failure_issue.add_issue_comment",
            AsyncMock(return_value=True),
        ) as add_comment,
        patch(
            "app.services.build_failure_issue.create_github_issue", AsyncMock()
        ) as create_issue,
    ):
        await BuildFailureIssueService().handle_result(pipeline, "failure")

    state = await get_state(service_database)
    assert state is not None
    assert state.issue_number == 8
    assert state.latest_sha == SHA_B
    assert state.latest_build_url == LOG_B
    add_comment.assert_awaited_once_with(
        "flathub/org.test.App",
        8,
        f"Another stable build failed.\n\nCommit SHA: {SHA_B}\nBuild log: {LOG_B}",
    )
    create_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_open_failure_updates_after_comment(service_database):
    await add_state(service_database)
    pipeline = make_pipeline(sha=SHA_B, log_url=LOG_B)
    with (
        patch(
            "app.services.build_failure_issue.get_github_issue",
            AsyncMock(return_value={"state": "open"}),
        ),
        patch(
            "app.services.build_failure_issue.add_issue_comment",
            AsyncMock(return_value=True),
        ) as add_comment,
        patch(
            "app.services.build_failure_issue.create_github_issue", AsyncMock()
        ) as create_issue,
    ):
        await BuildFailureIssueService().handle_result(pipeline, "failure")

    state = await get_state(service_database)
    assert state is not None
    assert state.issue_number == 10
    assert state.latest_sha == SHA_B
    assert state.latest_build_url == LOG_B
    add_comment.assert_awaited_once()
    create_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_comment_preserves_retry_metadata(service_database):
    await add_state(service_database)
    with (
        patch(
            "app.services.build_failure_issue.get_github_issue",
            AsyncMock(return_value={"state": "open"}),
        ),
        patch(
            "app.services.build_failure_issue.add_issue_comment",
            AsyncMock(return_value=False),
        ),
    ):
        await BuildFailureIssueService().handle_result(
            make_pipeline(sha=SHA_B, log_url=LOG_B), "failure"
        )

    state = await get_state(service_database)
    assert state is not None
    assert state.latest_sha == SHA_A
    assert state.latest_build_url == LOG_A


@pytest.mark.asyncio
async def test_closed_issue_creates_new_issue_on_failure(service_database):
    await add_state(service_database)
    with (
        patch(
            "app.services.build_failure_issue.get_github_issue",
            AsyncMock(return_value={"state": "closed"}),
        ),
        patch(
            "app.services.build_failure_issue.create_github_issue",
            AsyncMock(return_value=("https://github.com/issues/11", 11)),
        ) as create_issue,
    ):
        await BuildFailureIssueService().handle_result(
            make_pipeline(sha=SHA_B, log_url=LOG_B), "failure"
        )

    state = await get_state(service_database)
    assert state is not None
    assert state.issue_number == 11
    assert state.latest_sha == SHA_B
    assert state.latest_build_url == LOG_B
    create_issue.assert_awaited_once()


@pytest.mark.asyncio
async def test_issue_listing_failure_removes_sentinel(service_database):
    with (
        patch(
            "app.services.build_failure_issue.list_open_github_issues",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.build_failure_issue.create_github_issue", AsyncMock()
        ) as create_issue,
    ):
        await BuildFailureIssueService().handle_result(make_pipeline(), "failure")

    assert await get_state(service_database) is None
    create_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_issue_state_failure_preserves_active_issue(service_database):
    await add_state(service_database)
    with (
        patch(
            "app.services.build_failure_issue.get_github_issue",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.build_failure_issue.create_github_issue", AsyncMock()
        ) as create_issue,
    ):
        await BuildFailureIssueService().handle_result(
            make_pipeline(sha=SHA_B, log_url=LOG_B), "failure"
        )

    state = await get_state(service_database)
    assert state is not None
    assert state.issue_number == 10
    assert state.latest_sha == SHA_A
    create_issue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_from_issue", [None, 10])
async def test_success_comments_closes_and_clears_issue(
    service_database, retry_from_issue
):
    await add_state(service_database)
    pipeline = make_pipeline(
        sha=SHA_B, log_url=LOG_B, retry_from_issue=retry_from_issue
    )
    with (
        patch(
            "app.services.build_failure_issue.get_github_issue",
            AsyncMock(return_value={"state": "open"}),
        ),
        patch(
            "app.services.build_failure_issue.add_issue_comment",
            AsyncMock(return_value=True),
        ) as add_comment,
        patch(
            "app.services.build_failure_issue.close_github_issue",
            AsyncMock(return_value=True),
        ) as close_issue,
    ):
        await BuildFailureIssueService().handle_result(pipeline, "success")

    state = await get_state(service_database)
    assert state is not None
    assert state.issue_number is None
    assert state.latest_sha is None
    assert state.latest_build_url is None
    add_comment.assert_awaited_once_with(
        "flathub/org.test.App",
        10,
        f"A subsequent stable build succeeded.\n\nCommit SHA: {SHA_B}\nBuild log: {LOG_B}",
    )
    close_issue.assert_awaited_once_with("flathub/org.test.App", 10)


@pytest.mark.asyncio
async def test_success_close_failure_retains_issue(service_database):
    await add_state(service_database)
    with (
        patch(
            "app.services.build_failure_issue.get_github_issue",
            AsyncMock(return_value={"state": "open"}),
        ),
        patch(
            "app.services.build_failure_issue.add_issue_comment",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.services.build_failure_issue.close_github_issue",
            AsyncMock(return_value=False),
        ),
    ):
        await BuildFailureIssueService().handle_result(make_pipeline(), "success")

    state = await get_state(service_database)
    assert state is not None
    assert state.issue_number == 10
    assert state.latest_sha == SHA_A
    assert state.latest_build_url == LOG_A


@pytest.mark.asyncio
async def test_already_closed_success_clears_without_comment(service_database):
    await add_state(service_database)
    with (
        patch(
            "app.services.build_failure_issue.get_github_issue",
            AsyncMock(return_value={"state": "closed"}),
        ),
        patch(
            "app.services.build_failure_issue.add_issue_comment", AsyncMock()
        ) as add_comment,
        patch(
            "app.services.build_failure_issue.close_github_issue", AsyncMock()
        ) as close_issue,
    ):
        await BuildFailureIssueService().handle_result(make_pipeline(), "success")

    state = await get_state(service_database)
    assert state is not None
    assert state.issue_number is None
    add_comment.assert_not_awaited()
    close_issue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "flat_manager_repo", "repo"),
    [
        ("cancelled", "stable", "flathub/org.test.App"),
        ("failure", "beta", "flathub/org.test.App"),
        ("failure", "test", "flathub/org.test.App"),
        ("failure", "stable", ""),
    ],
)
async def test_ignored_results_are_noops(status, flat_manager_repo, repo):
    pipeline = make_pipeline(repo=repo, flat_manager_repo=flat_manager_repo)
    with patch("app.services.build_failure_issue.get_db") as get_db:
        await BuildFailureIssueService().handle_result(pipeline, status)
    get_db.assert_not_called()


@pytest.mark.asyncio
async def test_retry_params_use_latest_tracked_failure(service_database):
    await add_state(service_database, sha=SHA_B, log_url=LOG_B)
    with patch(
        "app.utils.github.get_workflow_run_title",
        AsyncMock(return_value="Build from refs/heads/branch/24.08"),
    ):
        params = await BuildFailureIssueService().get_retry_params(
            "flathub/org.test.App", "org.test.App", 10
        )

    assert params == {
        "sha": SHA_B,
        "repo": "flathub/org.test.App",
        "ref": "refs/heads/branch/24.08",
        "flat_manager_repo": "stable",
        "issue_type": "build_failure",
    }


@pytest.mark.asyncio
async def test_build_failure_issue_lifecycle_sequence(service_database):
    service = BuildFailureIssueService()
    failure_a = make_pipeline(sha=SHA_A, log_url=LOG_A)
    failure_b = make_pipeline(sha=SHA_B, log_url=LOG_B)
    success = make_pipeline(sha=SHA_B, log_url=LOG_B)
    failure_c = make_pipeline(
        sha="c" * 40,
        log_url="https://github.com/flathub-infra/vorarbeiter/actions/runs/300",
    )

    with (
        patch(
            "app.services.build_failure_issue.list_open_github_issues",
            AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.build_failure_issue.create_github_issue",
            AsyncMock(
                side_effect=[
                    ("https://github.com/issues/1", 1),
                    ("https://github.com/issues/2", 2),
                ]
            ),
        ) as create_issue,
        patch(
            "app.services.build_failure_issue.get_github_issue",
            AsyncMock(
                side_effect=[
                    {"state": "open"},
                    {"state": "open"},
                ]
            ),
        ),
        patch(
            "app.services.build_failure_issue.add_issue_comment",
            AsyncMock(return_value=True),
        ) as add_comment,
        patch(
            "app.services.build_failure_issue.close_github_issue",
            AsyncMock(return_value=True),
        ) as close_issue,
        patch(
            "app.utils.github.get_workflow_run_title",
            AsyncMock(return_value="Build from refs/heads/branch/24.08"),
        ),
    ):
        create_counts = []

        await service.handle_result(failure_a, "failure")
        create_counts.append(create_issue.await_count)

        original_body = create_issue.await_args_list[0].kwargs["body"]
        await service.handle_result(failure_b, "failure")
        create_counts.append(create_issue.await_count)

        retry_params = await service.get_retry_params(
            "flathub/org.test.App", "org.test.App", 1
        )
        assert retry_params is not None
        assert retry_params["sha"] == SHA_B
        assert retry_params["ref"] == "refs/heads/branch/24.08"

        await service.handle_result(success, "success")
        create_counts.append(create_issue.await_count)

        await service.handle_result(failure_c, "failure")
        create_counts.append(create_issue.await_count)

    assert create_counts == [1, 1, 1, 2]
    assert create_issue.await_args_list[0].kwargs["body"] == original_body
    assert f"Commit SHA: {SHA_A}" in original_body
    assert f"Commit SHA: {SHA_B}" in add_comment.await_args_list[0].args[2]
    assert (
        "A subsequent stable build succeeded." in add_comment.await_args_list[1].args[2]
    )
    close_issue.assert_awaited_once_with("flathub/org.test.App", 1)

    state = await get_state(service_database)
    assert state is not None
    assert state.issue_number == 2
    assert state.latest_sha == "c" * 40
