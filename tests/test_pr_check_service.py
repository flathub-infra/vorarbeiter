import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import app.services.submission_pr_check as pr_check_module
from app.services.submission_pr_check import (
    PRCheckService,
    get_domain,
    validate_title,
    appid_from_title,
)


@pytest.fixture(autouse=True)
def reset_template_cache():
    pr_check_module._PR_TEMPLATE_CACHE = None
    pr_check_module._PR_TEMPLATE_CACHE_TS = 0.0
    yield
    pr_check_module._PR_TEMPLATE_CACHE = None
    pr_check_module._PR_TEMPLATE_CACHE_TS = 0.0


def make_pr_payload(
    action="opened",
    base_ref="new-pr",
    pr_number=42,
    title="Add com.example.App",
    body="",
    draft=False,
    labels=None,
):
    return {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "title": title,
            "body": body,
            "draft": draft,
            "base": {"ref": base_ref},
            "labels": [{"name": label} for label in (labels or [])],
        },
    }


VALID_BODY = """
- [x] Please describe the application briefly. Great app
- [x] Please attach a video showcasing the application on Linux using the Flatpak. https://example.com/video
- [x] The Flatpak ID follows all the rules listed in the [Application ID requirements][appid].
- [x] I have read and followed all the [Submission requirements][reqs] and the [Submission guide][reqs2] and I agree to them.
- [x] I am an author/developer/upstream contributor to the project.
"""

TEMPLATE_ITEMS = [
    "Please describe the application briefly.",
    "Please attach a video showcasing the application on Linux using the Flatpak.",
    "The Flatpak ID follows all the rules listed in the [Application ID requirements][appid].",
    "I have read and followed all the [Submission requirements][reqs] and the [Submission guide][reqs2] and I agree to them.",
    "I am an",
]

VALID_FILES = ["com.example.myapp.yml"]


class TestGetDomain:
    @pytest.mark.parametrize(
        "appid",
        [
            "io.github.user.myapp",
            "com.gitlab.user.myapp",
            "org.gnome.myapp",
            "org.kde.myapp",
            "org.freedesktop.Platform.an_extension",
            "com.foobar.BaseApp",
            "com.example.plugin.foobar",
            "com.example.extensions.Bar",
            "com.example",
        ],
    )
    def test_domain_none(self, appid):
        assert get_domain(appid) is None

    @pytest.mark.parametrize(
        "appid, expected",
        [
            ("net.sourceforge.MyApp", "myapp.sourceforge.io"),
            ("page.codeberg.user.myapp", "user.codeberg.page"),
            ("com.example.App", "example.com"),
        ],
    )
    def test_domain_generation(self, appid, expected):
        assert get_domain(appid) == expected


class TestValidateTitle:
    @pytest.mark.parametrize(
        "title, expected",
        [
            ("Add com.example.myapp", True),
            ("add com.example.myapp", True),
            ("ADD com.example.myapp", True),
            ("com.example.myapp", False),
            ("Add com.example", False),
            ("", False),
            ("im llm", False),
        ],
    )
    def test_validate_title(self, title, expected):
        assert validate_title(title) == expected


class TestAppidFromTitle:
    @pytest.mark.parametrize(
        "title, expected",
        [
            ("Add com.example.myapp", "com.example.myapp"),
            ("add com.example.myapp", "com.example.myapp"),
            ("im llm", None),
            ("Add ", None),
        ],
    )
    def test_appid_from_title(self, title, expected):
        assert appid_from_title(title) == expected


class TestLatestBuildSucceeded:
    @pytest.mark.parametrize(
        "comments, expected",
        [
            (
                "Some comment\n[Test build succeeded](https://example.com)\n",
                True,
            ),
            (
                "[Test build succeeded](https://example.com)\n"
                "[Test build](https://example.com) failed.\n",
                False,
            ),
            ("Some random comment", False),
            ("", False),
        ],
    )
    def test_latest_build_succeeded(self, comments, expected):
        assert PRCheckService._latest_build_succeeded(comments) == expected


class TestIsItemAddressed:
    @pytest.mark.parametrize(
        "lines, expected",
        [
            (
                ["- [x] Please describe the application briefly. Some cool app"],
                True,
            ),
            (
                ["- [X] Please describe the application briefly. Some cool app"],
                True,
            ),
            (
                ["- [ ] Please describe the application briefly. N/A"],
                True,
            ),
            (
                ["- [ ] Please describe the application briefly."],
                False,
            ),
            (
                ["- [x] Some other item"],
                False,
            ),
            (
                ["- [] im llm"],
                False,
            ),
        ],
    )
    def test_is_item_addressed(self, lines, expected):
        assert (
            PRCheckService._is_item_addressed(
                "Please describe the application briefly.", lines
            )
            == expected
        )


class TestFetchChecklistItems:
    @pytest.mark.asyncio
    async def test_fetch_and_cache(self):
        template = "- [ ] Item one\n- [ ] Item two\n# Foobar\nSome foobar\n"
        mock_response = MagicMock()
        mock_response.text = template

        with patch("app.services.submission_pr_check.get_github_client") as mock:
            mock.return_value.request = AsyncMock(return_value=mock_response)
            items = await PRCheckService._fetch_checklist_items()

        assert items == ["Item one", "Item two"]
        assert pr_check_module._PR_TEMPLATE_CACHE == ["Item one", "Item two"]


class TestCheckChecklist:
    @pytest.mark.asyncio
    async def test_all_items_checked(self):
        service = PRCheckService()
        with patch.object(
            PRCheckService,
            "_fetch_checklist_items",
            AsyncMock(return_value=TEMPLATE_ITEMS),
        ):
            ok, reason = await service._check_checklist(VALID_BODY)
        assert ok is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_missing_item_fails(self):
        service = PRCheckService()
        body = "- [x] I am an author\n"
        with patch.object(
            PRCheckService,
            "_fetch_checklist_items",
            AsyncMock(return_value=TEMPLATE_ITEMS),
        ):
            ok, reason = await service._check_checklist(body)
        assert ok is False
        assert "checklists" in reason

    @pytest.mark.asyncio
    async def test_empty_template_fails(self):
        service = PRCheckService()
        with patch.object(
            PRCheckService, "_fetch_checklist_items", AsyncMock(return_value=[])
        ):
            ok, reason = await service._check_checklist(VALID_BODY)
        assert ok is False

    @pytest.mark.asyncio
    async def test_na_is_addressed(self):
        service = PRCheckService()
        body = "- [ ] Please describe the application briefly. N/A\n- [x] I am an\n"
        items = ["Please describe the application briefly.", "I am an"]
        with patch.object(
            PRCheckService, "_fetch_checklist_items", AsyncMock(return_value=items)
        ):
            ok, _ = await service._check_checklist(body)
        assert ok is True
