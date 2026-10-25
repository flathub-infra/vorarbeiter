from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.invite import parse_invite_command
from app.services.invite import InviteService


class TestParseInviteCommand:
    def test_parses_repo_and_deduplicates_users(self):
        cmd = parse_invite_command(
            "/invite repo=flathub/org.example.App @Alice @bob @alice\nthanks"
        )
        assert cmd is not None
        assert cmd.repo == "org.example.App"
        assert cmd.users == ["Alice", "bob"]

    @pytest.mark.parametrize(
        "comment",
        [
            "/invite repo=org.example.App",
            "/invite @alice",
            "/invite repo=org.example.App @flathub/reviewers",
            "/invite repo=org.example.App @alice extra",
            "please /invite repo=org.example.App @alice",
            "",
        ],
    )
    def test_rejects_invalid(self, comment):
        assert parse_invite_command(comment) is None


def _response(status_code, data=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = data or {}
    return response


async def _run(
    comment,
    *,
    author="reviewer",
    authorized=True,
    repo_response=None,
    appid="org.example.App",
    put_responses=(),
):
    client = AsyncMock()
    client.request = AsyncMock(
        side_effect=[
            repo_response or _response(200, {"default_branch": "master"}),
            *put_responses,
        ]
    )
    post_comment = AsyncMock()
    with (
        patch(
            "app.services.invite.is_reviewer_or_admin",
            new=AsyncMock(return_value=authorized),
        ),
        patch("app.services.invite.get_github_client", return_value=client),
        patch(
            "app.services.invite.detect_appid_from_github",
            new=AsyncMock(return_value=("manifest.json", appid)),
        ),
        patch("app.services.invite.add_issue_comment", new=post_comment),
    ):
        await InviteService().handle_invite_command(
            {
                "comment": {"body": comment, "user": {"login": author}},
                "issue": {"number": 7},
            }
        )
    puts = [c for c in client.request.call_args_list if c.args[0] == "put"]
    comments = [c.kwargs["comment"] for c in post_comment.call_args_list]
    return puts, comments


class TestInviteService:
    @pytest.mark.asyncio
    async def test_unauthorized_author_is_ignored(self):
        puts, comments = await _run(
            "/invite repo=org.example.App @alice", authorized=False
        )
        assert puts == []
        assert comments == []

    @pytest.mark.asyncio
    async def test_self_invite_refused_case_insensitively(self):
        puts, comments = await _run(
            "/invite repo=org.example.App @alice @Reviewer", author="reviewer"
        )
        assert puts == []
        assert "cannot invite yourself" in comments[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("repo_response", "appid"),
        [
            (_response(404), "org.example.App"),
            (_response(200, {"archived": True}), "org.example.App"),
            (_response(200, {"default_branch": "master"}), None),
        ],
        ids=["missing", "archived", "not-app"],
    )
    async def test_invalid_repo_refused(self, repo_response, appid):
        puts, comments = await _run(
            "/invite repo=org.example.App @alice",
            repo_response=repo_response,
            appid=appid,
        )
        assert puts == []
        assert comments[0].startswith("❌")

    @pytest.mark.asyncio
    async def test_reports_result_per_user(self):
        puts, comments = await _run(
            "/invite repo=org.example.App @alice @bob @carol",
            put_responses=[_response(201), _response(204), _response(404)],
        )
        assert [c.args[1] for c in puts] == [
            f"https://api.github.com/repos/flathub/org.example.App/collaborators/{u}"
            for u in ("alice", "bob", "carol")
        ]
        assert len(comments) == 1
        assert "✅ @alice" in comments[0]
        assert "ℹ️ @bob" in comments[0]
        assert "❌ @carol" in comments[0]
        assert "cc @flathub/reviewers" in comments[0]
