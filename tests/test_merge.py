import base64
import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.merge_request import MergeRequest, MergeStatus
from app.schemas.merge import parse_merge_command
from app.services.merge import (
    MERGE_ERROR_WORKFLOW_DISPATCH_FAILED,
    MergeCallbackConflictError,
    MergeInvalidTokenError,
    MergeNotFoundError,
    MergeService,
    _FinalizeContext,
)
from app.utils.manifest import _get_appid_from_manifest, _parse_manifest


def create_realistic_get_db(db_session_maker):
    @asynccontextmanager
    async def mock_get_db(*, use_replica=False):
        async with db_session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return mock_get_db


def _pr_details(**overrides):
    base = {
        "state": "open",
        "head_sha": "a" * 40,
        "fork_repo": "user/repo",
        "fork_branch": "main",
        "fork_clone_url": "https://github.com/user/repo.git",
        "author": "author1",
        "labels": [],
    }
    base.update(overrides)
    return base


class TestParseMergeCommand:
    def test_valid_merge_master(self):
        sha = "a" * 40
        cmd = parse_merge_command(f"/merge head={sha}")
        assert cmd is not None
        assert cmd.target_branch == "master"
        assert cmd.pr_head_sha == sha
        assert cmd.additional_collaborators == []

    def test_valid_merge_beta(self):
        sha = "b" * 40
        cmd = parse_merge_command(f"/merge:beta head={sha}")
        assert cmd is not None
        assert cmd.target_branch == "beta"
        assert cmd.pr_head_sha == sha

    def test_valid_merge_custom_branch(self):
        sha = "c" * 40
        cmd = parse_merge_command(f"/merge:24.08 head={sha}")
        assert cmd is not None
        assert cmd.target_branch == "branch/24.08"

    def test_merge_with_collaborators(self):
        sha = "d" * 40
        cmd = parse_merge_command(f"/merge head={sha} @user1 @user2")
        assert cmd is not None
        assert "user1" in cmd.additional_collaborators
        assert "user2" in cmd.additional_collaborators

    def test_merge_with_team_collaborators(self):
        sha = "e" * 40
        cmd = parse_merge_command(f"/merge head={sha} @flathub/KDE @user1")
        assert cmd is not None
        assert "flathub/KDE" in cmd.additional_collaborators
        assert "user1" in cmd.additional_collaborators

    def test_not_merge_command(self):
        assert parse_merge_command("bot, build") is None

    def test_invalid_format(self):
        assert parse_merge_command("/merge without proper format") is None

    def test_merge_no_head(self):
        assert parse_merge_command("/merge") is None

    def test_merge_short_sha(self):
        assert parse_merge_command("/merge head=abc123") is None


class TestManifestParsing:
    def test_parse_json_manifest(self):
        content = json.dumps(
            {"app-id": "org.example.App", "runtime": "org.freedesktop.Platform"}
        )
        result = _parse_manifest("org.example.App.json", content)
        assert result["app-id"] == "org.example.App"

    def test_parse_yaml_manifest(self):
        content = "app-id: org.example.App\nruntime: org.freedesktop.Platform\n"
        result = _parse_manifest("org.example.App.yml", content)
        assert result["app-id"] == "org.example.App"

    def test_parse_json_with_comments(self):
        content = '{\n  // This is a comment\n  "app-id": "org.example.App"\n}'
        result = _parse_manifest("org.example.App.json", content)
        assert result["app-id"] == "org.example.App"

    def test_parse_invalid_json(self):
        result = _parse_manifest("test.json", "not json at all {{{")
        assert result == {}

    def test_parse_invalid_yaml(self):
        result = _parse_manifest("test.yml", ":\n  :\n    - :\n      bad: [")
        assert result == {}

    def test_parse_json_with_block_comments(self):
        content = '{\n  /* block comment\n     spanning lines */\n  "app-id": "org.example.App"\n}'
        result = _parse_manifest("org.example.App.json", content)
        assert result["app-id"] == "org.example.App"

    def test_parse_json_preserves_comment_chars_in_strings(self):
        content = '{"app-id": "org.example.App", "url": "https://example.com//path"}'
        result = _parse_manifest("org.example.App.json", content)
        assert result["app-id"] == "org.example.App"
        assert result["url"] == "https://example.com//path"

    def test_parse_json_with_trailing_comma(self):
        content = '{\n  "app-id": "org.example.App",\n}'
        result = _parse_manifest("org.example.App.json", content)
        assert result["app-id"] == "org.example.App"

    def test_parse_json_with_single_quoted_strings(self):
        content = "{ 'app-id': 'org.example.App' }"
        result = _parse_manifest("org.example.App.json", content)
        assert result["app-id"] == "org.example.App"

    def test_parse_json_returns_empty_for_list(self):
        content = '[{"name": "cargo-sources"}]'
        result = _parse_manifest("cargo-sources.json", content)
        assert result == {}
        assert _get_appid_from_manifest(result) is None

    def test_parse_yaml_returns_empty_for_list(self):
        content = "- foo\n- bar\n"
        result = _parse_manifest("test.yml", content)
        assert result == {}
        assert _get_appid_from_manifest(result) is None

    def test_get_appid_app_id_key(self):
        assert (
            _get_appid_from_manifest({"app-id": "org.example.App"}) == "org.example.App"
        )

    def test_get_appid_id_key(self):
        assert _get_appid_from_manifest({"id": "org.example.App"}) == "org.example.App"

    def test_get_appid_missing(self):
        assert _get_appid_from_manifest({"runtime": "org.freedesktop.Platform"}) is None

    def test_get_appid_prefers_app_id(self):
        assert _get_appid_from_manifest({"app-id": "a", "id": "b"}) == "a"


class TestDetectAppidFromGithub:
    @pytest.mark.asyncio
    async def test_detect_appid_json(self):
        from app.utils.manifest import detect_appid_from_github

        manifest_content = json.dumps({"app-id": "org.example.App"})
        b64_content = base64.b64encode(manifest_content.encode()).decode()

        client = AsyncMock()

        list_response = MagicMock()
        list_response.json.return_value = [
            {"name": "org.example.App.json", "type": "file"},
            {"name": "README.md", "type": "file"},
        ]

        file_response = MagicMock()
        file_response.json.return_value = {"content": b64_content}

        client.request = AsyncMock(side_effect=[list_response, file_response])

        filename, appid = await detect_appid_from_github(client, "user/repo", "main")
        assert filename == "org.example.App.json"
        assert appid == "org.example.App"

    @pytest.mark.asyncio
    async def test_detect_appid_yaml(self):
        from app.utils.manifest import detect_appid_from_github

        manifest_content = (
            "app-id: org.example.App\nruntime: org.freedesktop.Platform\n"
        )
        b64_content = base64.b64encode(manifest_content.encode()).decode()

        client = AsyncMock()

        list_response = MagicMock()
        list_response.json.return_value = [
            {"name": "org.example.App.yml", "type": "file"},
        ]

        file_response = MagicMock()
        file_response.json.return_value = {"content": b64_content}

        client.request = AsyncMock(side_effect=[list_response, file_response])

        filename, appid = await detect_appid_from_github(client, "user/repo", "main")
        assert filename == "org.example.App.yml"
        assert appid == "org.example.App"

    @pytest.mark.asyncio
    async def test_detect_appid_no_manifests(self):
        from app.utils.manifest import detect_appid_from_github

        client = AsyncMock()
        list_response = MagicMock()
        list_response.json.return_value = [
            {"name": "README.md", "type": "file"},
        ]
        client.request = AsyncMock(return_value=list_response)

        filename, appid = await detect_appid_from_github(client, "user/repo", "main")
        assert filename is None
        assert appid is None

    @pytest.mark.asyncio
    async def test_detect_appid_name_mismatch(self):
        from app.utils.manifest import detect_appid_from_github

        manifest_content = json.dumps({"app-id": "org.example.App"})
        b64_content = base64.b64encode(manifest_content.encode()).decode()

        client = AsyncMock()

        list_response = MagicMock()
        list_response.json.return_value = [
            {"name": "wrong_name.json", "type": "file"},
        ]

        file_response = MagicMock()
        file_response.json.return_value = {"content": b64_content}

        client.request = AsyncMock(side_effect=[list_response, file_response])

        filename, appid = await detect_appid_from_github(client, "user/repo", "main")
        assert filename is None
        assert appid is None

    @pytest.mark.asyncio
    async def test_detect_appid_api_failure(self):
        from app.utils.manifest import detect_appid_from_github

        client = AsyncMock()
        client.request = AsyncMock(return_value=None)

        filename, appid = await detect_appid_from_github(client, "user/repo", "main")
        assert filename is None
        assert appid is None


class TestMergeServiceAuthorization:
    @pytest.mark.asyncio
    async def test_authorized_admin(self):
        service = MergeService()

        admin_response = MagicMock()
        admin_response.status_code = 200
        admin_response.json.return_value = {"state": "active", "role": "member"}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=admin_response)

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._is_authorized("admin_user")
            assert result is True

        url = mock_client.request.call_args.args[1]
        assert "/memberships/" in url

    @pytest.mark.asyncio
    async def test_authorized_reviewer(self):
        service = MergeService()

        not_admin = MagicMock()
        not_admin.status_code = 404

        reviewer_response = MagicMock()
        reviewer_response.status_code = 200
        reviewer_response.json.return_value = {"state": "active", "role": "member"}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=[not_admin, reviewer_response])

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._is_authorized("reviewer_user")
            assert result is True

    @pytest.mark.asyncio
    async def test_unauthorized(self):
        service = MergeService()

        not_member = MagicMock()
        not_member.status_code = 404

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=not_member)

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._is_authorized("random_user")
            assert result is False

    @pytest.mark.asyncio
    async def test_pending_membership_not_authorized(self):
        service = MergeService()

        pending_response = MagicMock()
        pending_response.status_code = 200
        pending_response.json.return_value = {"state": "pending", "role": "member"}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=pending_response)

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._is_authorized("invited_user")
            assert result is False


class TestMergeServiceCollaborators:
    @pytest.mark.asyncio
    async def test_add_collaborators_deduplicates_users_and_teams(self):
        service = MergeService()

        success_response = MagicMock()
        success_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=success_response)

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._add_collaborators(
                "org.kde.Dolphin",
                [
                    "user1",
                    "user1",
                    "flathub/KDE",
                    "flathub/trusted-maintainers",
                ],
            )
            assert result is True

            urls = [call.args[1] for call in mock_client.request.call_args_list]
            assert (
                urls.count(
                    "https://api.github.com/repos/flathub/org.kde.Dolphin/collaborators/user1"
                )
                == 1
            )
            assert (
                urls.count(
                    "https://api.github.com/orgs/flathub/teams/KDE/repos/flathub/org.kde.Dolphin"
                )
                == 1
            )
            assert (
                urls.count(
                    "https://api.github.com/orgs/flathub/teams/trusted-maintainers/repos/flathub/org.kde.Dolphin"
                )
                == 1
            )

    @pytest.mark.asyncio
    async def test_add_collaborators_with_kde_team(self):
        service = MergeService()

        success_response = MagicMock()
        success_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=success_response)

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._add_collaborators("org.kde.Dolphin", ["user1"])
            assert result is True

            calls = mock_client.request.call_args_list
            urls = [call.args[1] for call in calls]
            assert any("teams/KDE" in url for url in urls)
            assert any("teams/trusted-maintainers" in url for url in urls)
            assert any("collaborators/user1" in url for url in urls)

    @pytest.mark.asyncio
    async def test_add_collaborators_with_gnome_team(self):
        service = MergeService()

        success_response = MagicMock()
        success_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=success_response)

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._add_collaborators("org.gnome.Nautilus", ["user1"])
            assert result is True

            calls = mock_client.request.call_args_list
            urls = [call.args[1] for call in calls]
            assert any("teams/GNOME" in url for url in urls)

    @pytest.mark.asyncio
    async def test_gnome_team_not_added_for_deep_namespace(self):
        service = MergeService()

        success_response = MagicMock()
        success_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=success_response)

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._add_collaborators(
                "org.gnome.World.Secrets", ["user1"]
            )
            assert result is True

            calls = mock_client.request.call_args_list
            urls = [call.args[1] for call in calls]
            assert not any("teams/GNOME" in url for url in urls)

    @pytest.mark.asyncio
    async def test_add_team_collaborator_from_command(self):
        service = MergeService()

        success_response = MagicMock()
        success_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=success_response)

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._add_collaborators(
                "com.example.App", ["user1", "flathub/custom-team"]
            )
            assert result is True

            calls = mock_client.request.call_args_list
            urls = [call.args[1] for call in calls]
            assert any("teams/custom-team" in url for url in urls)


class TestMergeServiceCloseAndLock:
    @pytest.mark.asyncio
    async def test_skips_lock_when_already_locked(self):
        service = MergeService()

        comment_response = MagicMock()
        comment_response.status_code = 201
        close_response = MagicMock()
        close_response.status_code = 200
        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = {"locked": True}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(
            side_effect=[comment_response, close_response, issue_response]
        )

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._close_and_lock_pr(
                123, "https://github.com/flathub/org.example.App"
            )

        assert result is True
        methods = [call.args[0] for call in mock_client.request.call_args_list]
        urls = [call.args[1] for call in mock_client.request.call_args_list]
        assert "put" not in methods
        assert not any(url.endswith("/lock") for url in urls)

    @pytest.mark.asyncio
    async def test_locks_when_not_locked(self):
        service = MergeService()

        comment_response = MagicMock()
        comment_response.status_code = 201
        close_response = MagicMock()
        close_response.status_code = 200
        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = {"locked": False}
        lock_response = MagicMock()
        lock_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(
            side_effect=[
                comment_response,
                close_response,
                issue_response,
                lock_response,
            ]
        )

        with patch.object(service, "_get_client", return_value=mock_client):
            result = await service._close_and_lock_pr(
                123, "https://github.com/flathub/org.example.App"
            )

        assert result is True
        urls = [call.args[1] for call in mock_client.request.call_args_list]
        assert any(url.endswith("/lock") for url in urls)


class TestMergeServiceProcess:
    @pytest.mark.asyncio
    async def test_process_persists_row_before_repo_creation(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()

        mock_get_db = create_realistic_get_db(db_session_maker)

        async def assert_repo_created_after_persist(appid: str) -> str:
            async with db_session_maker() as session:
                mr = await session.get(MergeRequest, merge_id)
                assert mr is not None
                assert mr.status == MergeStatus.PUSHING
                assert mr.repo_html_url is None
            return f"https://github.com/flathub/{appid}"

        with (
            patch("app.services.merge.get_db", side_effect=lambda: mock_get_db()),
            patch.object(service, "_is_authorized", new=AsyncMock(return_value=True)),
            patch.object(
                service,
                "_get_pr_details",
                new=AsyncMock(return_value=_pr_details()),
            ),
            patch(
                "app.services.merge.detect_appid_from_github",
                new=AsyncMock(return_value=("org.example.App.yml", "org.example.App")),
            ),
            patch.object(
                service, "_has_active_merge_request", new=AsyncMock(return_value=False)
            ),
            patch.object(
                service, "_check_repo_exists", new=AsyncMock(return_value=False)
            ),
            patch.object(
                service, "_create_repo", side_effect=assert_repo_created_after_persist
            ),
            patch.object(service, "_dispatch_merge_workflow", new=AsyncMock()),
            patch.object(uuid, "uuid4", side_effect=[merge_id]),
        ):
            await service._process_merge(
                comment_body=f"/merge head={'a' * 40}",
                pr_number=123,
                comment_author="reviewer",
            )

    @pytest.mark.asyncio
    async def test_process_sets_pushing_before_dispatch(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()

        mock_get_db = create_realistic_get_db(db_session_maker)

        async def assert_dispatch_after_pushing(merge_request: MergeRequest) -> None:
            async with db_session_maker() as session:
                mr = await session.get(MergeRequest, merge_id)
                assert mr is not None
                assert mr.status == MergeStatus.PUSHING
                assert mr.repo_html_url == "https://github.com/flathub/org.example.App"
            assert merge_request.status == MergeStatus.PUSHING

        with (
            patch("app.services.merge.get_db", side_effect=lambda: mock_get_db()),
            patch.object(service, "_is_authorized", new=AsyncMock(return_value=True)),
            patch.object(
                service,
                "_get_pr_details",
                new=AsyncMock(return_value=_pr_details()),
            ),
            patch(
                "app.services.merge.detect_appid_from_github",
                new=AsyncMock(return_value=("org.example.App.yml", "org.example.App")),
            ),
            patch.object(
                service, "_has_active_merge_request", new=AsyncMock(return_value=False)
            ),
            patch.object(
                service, "_check_repo_exists", new=AsyncMock(return_value=False)
            ),
            patch.object(
                service,
                "_create_repo",
                new=AsyncMock(
                    return_value="https://github.com/flathub/org.example.App"
                ),
            ),
            patch.object(
                service,
                "_dispatch_merge_workflow",
                side_effect=assert_dispatch_after_pushing,
            ),
            patch.object(uuid, "uuid4", side_effect=[merge_id]),
        ):
            await service._process_merge(
                comment_body=f"/merge head={'a' * 40}",
                pr_number=123,
                comment_author="reviewer",
            )

    @pytest.mark.asyncio
    async def test_process_repo_creation_failure_marks_failed(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()

        mock_get_db = create_realistic_get_db(db_session_maker)

        with (
            patch("app.services.merge.get_db", side_effect=lambda: mock_get_db()),
            patch.object(service, "_is_authorized", new=AsyncMock(return_value=True)),
            patch.object(
                service,
                "_get_pr_details",
                new=AsyncMock(return_value=_pr_details()),
            ),
            patch(
                "app.services.merge.detect_appid_from_github",
                new=AsyncMock(return_value=("org.example.App.yml", "org.example.App")),
            ),
            patch.object(
                service, "_has_active_merge_request", new=AsyncMock(return_value=False)
            ),
            patch.object(
                service, "_check_repo_exists", new=AsyncMock(return_value=False)
            ),
            patch.object(service, "_create_repo", new=AsyncMock(return_value=None)),
            patch.object(service, "_dispatch_merge_workflow", new=AsyncMock()),
            patch.object(service, "_post_comment", new=AsyncMock()) as mock_comment,
            patch.object(uuid, "uuid4", side_effect=[merge_id]),
        ):
            await service._process_merge(
                comment_body=f"/merge head={'a' * 40}",
                pr_number=123,
                comment_author="reviewer",
            )

        async with db_session_maker() as session:
            mr = await session.get(MergeRequest, merge_id)
            assert mr is not None
            assert mr.status == MergeStatus.FAILED
            assert mr.error == "Failed to create repository"
            assert mr.completed_at is not None

        mock_comment.assert_called_once_with(
            123,
            "❌ Failed to create repository `flathub/org.example.App`.",
        )

    @pytest.mark.asyncio
    async def test_process_dispatch_failure_marks_failed(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()

        mock_get_db = create_realistic_get_db(db_session_maker)

        with (
            patch("app.services.merge.get_db", side_effect=lambda: mock_get_db()),
            patch.object(service, "_is_authorized", new=AsyncMock(return_value=True)),
            patch.object(
                service,
                "_get_pr_details",
                new=AsyncMock(return_value=_pr_details()),
            ),
            patch(
                "app.services.merge.detect_appid_from_github",
                new=AsyncMock(return_value=("org.example.App.yml", "org.example.App")),
            ),
            patch.object(
                service, "_has_active_merge_request", new=AsyncMock(return_value=False)
            ),
            patch.object(
                service, "_check_repo_exists", new=AsyncMock(return_value=False)
            ),
            patch.object(
                service,
                "_create_repo",
                new=AsyncMock(
                    return_value="https://github.com/flathub/org.example.App"
                ),
            ),
            patch.object(
                service,
                "_dispatch_merge_workflow",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(service, "_post_comment", new=AsyncMock()) as mock_comment,
            patch.object(uuid, "uuid4", side_effect=[merge_id]),
        ):
            await service._process_merge(
                comment_body=f"/merge head={'a' * 40}",
                pr_number=123,
                comment_author="reviewer",
            )

        async with db_session_maker() as session:
            mr = await session.get(MergeRequest, merge_id)
            assert mr is not None
            assert mr.status == MergeStatus.FAILED
            assert mr.error == MERGE_ERROR_WORKFLOW_DISPATCH_FAILED
            assert mr.completed_at is not None
            assert mr.repo_html_url == "https://github.com/flathub/org.example.App"

        mock_comment.assert_called_once_with(
            123,
            "❌ Failed to dispatch merge workflow.",
        )

    @pytest.mark.asyncio
    async def test_process_retries_after_dispatch_failure_with_existing_repo(
        self, db_session_maker
    ):
        service = MergeService()
        failed_merge_id = uuid.uuid4()
        retry_merge_id = uuid.uuid4()

        async with db_session_maker() as session:
            session.add(
                MergeRequest(
                    id=failed_merge_id,
                    pr_number=123,
                    app_id="org.example.App",
                    target_branch="master",
                    pr_head_sha="a" * 40,
                    collaborators=["user1"],
                    status=MergeStatus.FAILED,
                    callback_token="failed_token",
                    comment_author="reviewer",
                    fork_url="https://github.com/user/repo.git",
                    fork_branch="main",
                    repo_html_url="https://github.com/flathub/org.example.App",
                    error=MERGE_ERROR_WORKFLOW_DISPATCH_FAILED,
                )
            )
            await session.commit()

        mock_get_db = create_realistic_get_db(db_session_maker)

        with (
            patch(
                "app.services.merge.get_db",
                side_effect=lambda *args, **kwargs: mock_get_db(*args, **kwargs),
            ),
            patch.object(service, "_is_authorized", new=AsyncMock(return_value=True)),
            patch.object(
                service,
                "_get_pr_details",
                new=AsyncMock(return_value=_pr_details()),
            ),
            patch(
                "app.services.merge.detect_appid_from_github",
                new=AsyncMock(return_value=("org.example.App.yml", "org.example.App")),
            ),
            patch.object(
                service, "_check_repo_exists", new=AsyncMock(return_value=True)
            ),
            patch.object(service, "_create_repo", new=AsyncMock()) as mock_create_repo,
            patch.object(service, "_dispatch_merge_workflow", new=AsyncMock()),
            patch.object(uuid, "uuid4", side_effect=[retry_merge_id]),
        ):
            await service._process_merge(
                comment_body=f"/merge head={'a' * 40}",
                pr_number=123,
                comment_author="reviewer",
            )

        mock_create_repo.assert_not_called()

        async with db_session_maker() as session:
            rows = (
                (
                    await session.execute(
                        select(MergeRequest).where(MergeRequest.pr_number == 123)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 2
            retry_row = next(row for row in rows if row.id == retry_merge_id)
            assert retry_row.status == MergeStatus.PUSHING
            assert (
                retry_row.repo_html_url == "https://github.com/flathub/org.example.App"
            )

    @pytest.mark.asyncio
    async def test_process_rejects_duplicate_active_merge_before_repo_check(
        self, db_session_maker
    ):
        service = MergeService()

        with (
            patch.object(service, "_is_authorized", new=AsyncMock(return_value=True)),
            patch.object(
                service,
                "_get_pr_details",
                new=AsyncMock(return_value=_pr_details()),
            ),
            patch(
                "app.services.merge.detect_appid_from_github",
                new=AsyncMock(return_value=("org.example.App.yml", "org.example.App")),
            ),
            patch.object(
                service, "_has_active_merge_request", new=AsyncMock(return_value=True)
            ),
            patch.object(
                service, "_check_repo_exists", new=AsyncMock(return_value=True)
            ) as mock_repo_exists,
            patch.object(service, "_create_repo", new=AsyncMock()),
            patch.object(service, "_post_comment", new=AsyncMock()) as mock_comment,
        ):
            await service._process_merge(
                comment_body=f"/merge head={'a' * 40}",
                pr_number=123,
                comment_author="reviewer",
            )

        mock_comment.assert_called_once_with(
            123,
            "❌ A merge operation is already in progress for this PR.",
        )
        mock_repo_exists.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_rejects_pr_with_blocking_label(self, db_session_maker):
        service = MergeService()

        with (
            patch.object(service, "_is_authorized", new=AsyncMock(return_value=True)),
            patch.object(
                service,
                "_get_pr_details",
                new=AsyncMock(
                    return_value=_pr_details(
                        labels=["needs-review", "blocked-by-rebase"]
                    )
                ),
            ),
            patch(
                "app.services.merge.detect_appid_from_github", new=AsyncMock()
            ) as mock_detect,
            patch.object(service, "_post_comment", new=AsyncMock()) as mock_comment,
        ):
            await service._process_merge(
                comment_body=f"/merge head={'a' * 40}",
                pr_number=123,
                comment_author="reviewer",
            )

        mock_detect.assert_not_called()
        mock_comment.assert_called_once_with(
            123,
            "❌ Cannot merge: PR has blocking labels: `blocked-by-rebase`",
        )

    @pytest.mark.asyncio
    async def test_has_active_merge_request_accepts_legacy_creating_state(
        self, db_session_maker
    ):
        service = MergeService()

        async with db_session_maker() as session:
            session.add(
                MergeRequest(
                    id=uuid.uuid4(),
                    pr_number=123,
                    app_id="org.example.App",
                    target_branch="master",
                    pr_head_sha="b" * 40,
                    collaborators=["user1"],
                    status=MergeStatus.CREATING,
                    comment_author="reviewer",
                    fork_url="https://github.com/user/repo.git",
                    fork_branch="main",
                )
            )
            await session.commit()

        mock_get_db = create_realistic_get_db(db_session_maker)

        with patch(
            "app.services.merge.get_db",
            side_effect=lambda *args, **kwargs: mock_get_db(*args, **kwargs),
        ):
            assert await service._has_active_merge_request(123) is True

    @pytest.mark.asyncio
    async def test_has_active_merge_request_uses_writer_session(self):
        service = MergeService()
        mock_session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=result)

        with patch("app.services.merge.get_db") as mock_get_db_factory:
            from tests.conftest import create_mock_get_db

            mock_get_db_factory.return_value = create_mock_get_db(mock_session)()

            assert await service._has_active_merge_request(123) is False

        assert mock_get_db_factory.call_args.kwargs == {}


class TestMergeServiceFinalize:
    @staticmethod
    def _make_ctx(merge_id):
        return _FinalizeContext(
            merge_id=merge_id,
            pr_number=123,
            app_id="org.example.App",
            target_branch="master",
            pr_head_sha="a" * 40,
            collaborators=["author1"],
            repo_html_url="https://github.com/flathub/org.example.App",
        )

    @staticmethod
    async def _seed(db_session_maker, merge_id):
        async with db_session_maker() as session:
            session.add(
                MergeRequest(
                    id=merge_id,
                    pr_number=123,
                    app_id="org.example.App",
                    target_branch="master",
                    pr_head_sha="a" * 40,
                    collaborators=["author1"],
                    status=MergeStatus.FINALIZING,
                    callback_token="t",
                    comment_author="reviewer",
                    fork_url="https://github.com/user/repo.git",
                    fork_branch="main",
                    repo_html_url="https://github.com/flathub/org.example.App",
                )
            )
            await session.commit()

    @pytest.mark.asyncio
    async def test_finalize_closes_and_locks_pr_when_no_errors(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()
        await self._seed(db_session_maker, merge_id)
        mock_get_db = create_realistic_get_db(db_session_maker)

        with (
            patch("app.services.merge.get_db", side_effect=lambda: mock_get_db()),
            patch.object(
                service, "_remove_collaborator", new=AsyncMock(return_value=True)
            ),
            patch.object(
                service,
                "_set_all_branch_protections",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                service, "_add_collaborators", new=AsyncMock(return_value=True)
            ),
            patch.object(
                service,
                "_verify_branch_state",
                new=AsyncMock(return_value=(True, True)),
            ),
            patch.object(service, "_set_labels", new=AsyncMock(return_value=True)),
            patch.object(
                service, "_clear_pr_metadata", new=AsyncMock(return_value=True)
            ),
            patch.object(
                service, "_close_and_lock_pr", new=AsyncMock(return_value=True)
            ) as mock_close,
            patch.object(service, "_post_comment", new=AsyncMock()) as mock_comment,
        ):
            await service._finalize(self._make_ctx(merge_id))

        mock_close.assert_called_once_with(
            123, "https://github.com/flathub/org.example.App"
        )
        mock_comment.assert_not_called()

        async with db_session_maker() as session:
            mr = await session.get(MergeRequest, merge_id)
            assert mr is not None
            assert mr.status == MergeStatus.COMPLETED
            assert mr.error is None

    @pytest.mark.asyncio
    async def test_finalize_skips_close_and_lock_when_errors(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()
        await self._seed(db_session_maker, merge_id)
        mock_get_db = create_realistic_get_db(db_session_maker)

        with (
            patch("app.services.merge.get_db", side_effect=lambda: mock_get_db()),
            patch.object(
                service, "_remove_collaborator", new=AsyncMock(return_value=True)
            ),
            patch.object(
                service,
                "_set_all_branch_protections",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                service, "_add_collaborators", new=AsyncMock(return_value=True)
            ),
            patch.object(
                service,
                "_verify_branch_state",
                new=AsyncMock(return_value=(True, True)),
            ),
            patch.object(service, "_set_labels", new=AsyncMock(return_value=True)),
            patch.object(
                service, "_clear_pr_metadata", new=AsyncMock(return_value=True)
            ),
            patch.object(
                service, "_close_and_lock_pr", new=AsyncMock(return_value=True)
            ) as mock_close,
            patch.object(service, "_post_comment", new=AsyncMock()) as mock_comment,
        ):
            await service._finalize(self._make_ctx(merge_id))

        mock_close.assert_not_called()
        mock_comment.assert_called_once()
        body = mock_comment.call_args.args[1]
        assert body.startswith("⚠️ Merge finalization completed with errors")
        assert "branch protections" in body

        async with db_session_maker() as session:
            mr = await session.get(MergeRequest, merge_id)
            assert mr is not None
            assert mr.status == MergeStatus.COMPLETED
            assert mr.error is not None
            assert "branch protections" in mr.error


class TestMergeCallback:
    @pytest.mark.asyncio
    async def test_callback_success(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()
        token = "test_token_abc"

        async with db_session_maker() as session:
            mr = MergeRequest(
                id=merge_id,
                pr_number=123,
                app_id="org.example.App",
                target_branch="master",
                pr_head_sha="a" * 40,
                collaborators=["user1"],
                status=MergeStatus.PUSHING,
                callback_token=token,
                comment_author="reviewer",
                fork_url="https://github.com/user/repo.git",
                fork_branch="main",
                repo_html_url="https://github.com/flathub/org.example.App",
            )
            session.add(mr)
            await session.commit()

        with (
            patch("app.services.merge.get_db") as mock_get_db_factory,
            patch.object(service, "_finalize", new_callable=AsyncMock) as mock_finalize,
        ):
            from tests.conftest import create_mock_get_db

            mock_session = AsyncMock(spec=AsyncSession)
            mock_mr = MagicMock()
            mock_mr.status = MergeStatus.PUSHING
            mock_mr.callback_token = token
            mock_mr.pr_number = 123

            mock_session.get = AsyncMock(return_value=mock_mr)
            mock_get_db_factory.return_value = create_mock_get_db(
                mock_session
            ).__wrapped__(use_replica=False)

            mock_get_db = create_realistic_get_db(db_session_maker)

            mock_get_db_factory.side_effect = None
            mock_get_db_factory.return_value = mock_get_db()

            await service.handle_callback(merge_id, token, "success")
            mock_finalize.assert_called_once()

    @pytest.mark.asyncio
    async def test_callback_invalid_token(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()

        async with db_session_maker() as session:
            mr = MergeRequest(
                id=merge_id,
                pr_number=123,
                app_id="org.example.App",
                target_branch="master",
                pr_head_sha="a" * 40,
                collaborators=["user1"],
                status=MergeStatus.PUSHING,
                callback_token="correct_token",
                comment_author="reviewer",
                fork_url="https://github.com/user/repo.git",
                fork_branch="main",
            )
            session.add(mr)
            await session.commit()

        mock_get_db = create_realistic_get_db(db_session_maker)

        with (
            patch("app.services.merge.get_db", side_effect=lambda: mock_get_db()),
            patch.object(service, "_finalize", new_callable=AsyncMock) as mock_finalize,
        ):
            with pytest.raises(MergeInvalidTokenError):
                await service.handle_callback(merge_id, "wrong_token", "success")
            mock_finalize.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_missing_merge_raises_not_found(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()

        mock_get_db = create_realistic_get_db(db_session_maker)

        with patch("app.services.merge.get_db", side_effect=lambda: mock_get_db()):
            with pytest.raises(MergeNotFoundError):
                await service.handle_callback(merge_id, "token", "success")

    @pytest.mark.asyncio
    async def test_callback_wrong_state_raises_conflict(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()

        async with db_session_maker() as session:
            session.add(
                MergeRequest(
                    id=merge_id,
                    pr_number=123,
                    app_id="org.example.App",
                    target_branch="master",
                    pr_head_sha="a" * 40,
                    collaborators=["user1"],
                    status=MergeStatus.CREATING,
                    callback_token="correct_token",
                    comment_author="reviewer",
                    fork_url="https://github.com/user/repo.git",
                    fork_branch="main",
                )
            )
            await session.commit()

        mock_get_db = create_realistic_get_db(db_session_maker)

        with patch("app.services.merge.get_db", side_effect=lambda: mock_get_db()):
            with pytest.raises(MergeCallbackConflictError):
                await service.handle_callback(merge_id, "correct_token", "success")

    @pytest.mark.asyncio
    async def test_callback_failure_marks_failed(self, db_session_maker):
        service = MergeService()
        merge_id = uuid.uuid4()

        async with db_session_maker() as session:
            session.add(
                MergeRequest(
                    id=merge_id,
                    pr_number=123,
                    app_id="org.example.App",
                    target_branch="master",
                    pr_head_sha="a" * 40,
                    collaborators=["user1"],
                    status=MergeStatus.PUSHING,
                    callback_token="correct_token",
                    comment_author="reviewer",
                    fork_url="https://github.com/user/repo.git",
                    fork_branch="main",
                )
            )
            await session.commit()

        mock_get_db = create_realistic_get_db(db_session_maker)

        with (
            patch("app.services.merge.get_db", side_effect=lambda: mock_get_db()),
            patch.object(service, "_post_comment", new=AsyncMock()) as mock_comment,
            patch.object(service, "_finalize", new_callable=AsyncMock) as mock_finalize,
        ):
            await service.handle_callback(merge_id, "correct_token", "failure")

        async with db_session_maker() as session:
            mr = await session.get(MergeRequest, merge_id)
            assert mr is not None
            assert mr.status == MergeStatus.FAILED
            assert mr.error == "Git push workflow failed"
            assert mr.completed_at is not None

        mock_finalize.assert_not_called()
        mock_comment.assert_called_once_with(
            123,
            "❌ Merge failed: git push workflow reported failure.",
        )


class TestMergeCallbackRoute:
    def test_callback_rejects_non_object_json(self, client):
        merge_id = uuid.uuid4()
        response = client.post(
            f"/api/merge/{merge_id}/callback",
            json=["success"],
            headers={"Authorization": "Bearer test_token"},
        )
        assert response.status_code == 400

    def test_callback_missing_auth(self, client):
        merge_id = uuid.uuid4()
        response = client.post(
            f"/api/merge/{merge_id}/callback",
            json={"status": "success"},
        )
        assert response.status_code == 401

    def test_callback_invalid_status(self, client):
        merge_id = uuid.uuid4()
        response = client.post(
            f"/api/merge/{merge_id}/callback",
            json={"status": "invalid"},
            headers={"Authorization": "Bearer test_token"},
        )
        assert response.status_code == 400

    def test_callback_valid_request(self, client):
        merge_id = uuid.uuid4()
        with patch(
            "app.services.merge_service.handle_callback",
            new_callable=AsyncMock,
        ) as mock_callback:
            response = client.post(
                f"/api/merge/{merge_id}/callback",
                json={"status": "success"},
                headers={"Authorization": "Bearer test_token"},
            )
            assert response.status_code == 200
            mock_callback.assert_called_once_with(merge_id, "test_token", "success")

    def test_callback_not_found(self, client):
        merge_id = uuid.uuid4()
        with patch(
            "app.services.merge_service.handle_callback",
            new=AsyncMock(side_effect=MergeNotFoundError("missing")),
        ):
            response = client.post(
                f"/api/merge/{merge_id}/callback",
                json={"status": "success"},
                headers={"Authorization": "Bearer test_token"},
            )
        assert response.status_code == 404

    def test_callback_invalid_token(self, client):
        merge_id = uuid.uuid4()
        with patch(
            "app.services.merge_service.handle_callback",
            new=AsyncMock(side_effect=MergeInvalidTokenError("bad token")),
        ):
            response = client.post(
                f"/api/merge/{merge_id}/callback",
                json={"status": "success"},
                headers={"Authorization": "Bearer test_token"},
            )
        assert response.status_code == 401

    def test_callback_conflict(self, client):
        merge_id = uuid.uuid4()
        with patch(
            "app.services.merge_service.handle_callback",
            new=AsyncMock(side_effect=MergeCallbackConflictError("wrong state")),
        ):
            response = client.post(
                f"/api/merge/{merge_id}/callback",
                json={"status": "success"},
                headers={"Authorization": "Bearer test_token"},
            )
        assert response.status_code == 409
