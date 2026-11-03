from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.services.build_failure import (
    build_failure_summary,
    extract_diagnostic,
    failed_step_log,
    literal,
)


def test_module_diagnostic_ignores_other_modules_and_cascading_errors():
    result = extract_diagnostic(
        "error: earlier unrelated probe\n"
        "========================================================================\n"
        "Building module widget in /run/build/widget\n"
        "compiling widget.c\n"
        "\x1b[31mwidget.c:4: fatal error: missing.h: No such file\x1b[0m\n"
        "    #include <missing.h>\n"
        "make: *** [all] Error 2\n"
        "Error: module widget: Child process exited with code 2\n"
        "error: later cleanup failed"
    )
    assert result.module == "widget"
    assert result.excerpt is not None
    assert "missing.h" in result.excerpt
    assert "earlier" not in result.excerpt
    assert "cleanup" not in result.excerpt
    assert "make:" not in result.excerpt
    assert "\x1b" not in result.excerpt


@pytest.mark.parametrize(
    "log",
    [
        "ninja: build stopped: subcommand failed.\nProcess completed with exit code 1",
        "=== Building module widget in /run/build/widget\nunknown problem",
        "error: Recipe `build` failed on line 10 with exit code 1",
    ],
)
def test_unknown_diagnostic_is_not_guessed(log):
    assert extract_diagnostic(log).excerpt is None
    assert extract_diagnostic(log).module is None


def test_explicit_module_without_context_is_not_assigned_other_errors():
    result = extract_diagnostic("error: unrelated\nError: module widget: exit code 1")
    assert result.module == "widget"
    assert result.excerpt is None


def test_excerpt_is_bounded_and_markdown_stays_literal():
    result = extract_diagnostic("error: " + "x" * 3000)
    assert result.excerpt is not None
    assert len(result.excerpt) == 2000
    assert literal("```\n@someone\n<script>") == "    ```\n    @someone\n    <script>"


def test_long_compiler_command_does_not_displace_diagnostic():
    diagnostic = "widget.c:4: fatal error: missing.h: No such file"
    result = extract_diagnostic(
        "cc " + "-Iinclude/path " * 250 + "\n" + diagnostic + "\n#include <missing.h>"
    )
    assert result.excerpt is not None
    assert diagnostic in result.excerpt
    assert "#include <missing.h>" in result.excerpt
    assert len(result.excerpt) <= 2000
    assert len(result.excerpt.splitlines()) <= 12


@pytest.mark.parametrize("marker", ["module widget:", "module: widget:"])
@pytest.mark.parametrize(
    "reason",
    [
        'Invalid buildsystem: "typo"',
        "Can't find meson.build",
        "Can't find CMakeLists.txt",
    ],
)
@pytest.mark.parametrize("has_start", [False, True])
def test_specific_module_failure_is_retained(marker, reason, has_start):
    log = "error: unrelated earlier probe\n"
    if has_start:
        log += "Building module widget in /run/build/widget\n"
    log += f"Error: {marker} {reason}\nerror: later cleanup failed"
    result = extract_diagnostic(log)
    assert result.module == "widget"
    assert result.excerpt is not None
    assert reason in result.excerpt
    assert "unrelated" not in result.excerpt
    assert "cleanup" not in result.excerpt


@pytest.mark.parametrize("marker", ["module widget:", "module: widget:"])
@pytest.mark.parametrize("reason", ["Child process exited with code 2", "exit code 1"])
def test_generic_module_failure_is_not_used_as_diagnostic(marker, reason):
    result = extract_diagnostic(
        f"Building module widget in /run/build/widget\nError: {marker} {reason}"
    )
    assert result.module == "widget"
    assert result.excerpt is None


STEPS = [
    {
        "conclusion": "failure",
        "started_at": "2026-10-31T17:32:57Z",
        "completed_at": "2026-10-31T17:33:04Z",
    }
]
LOG = (
    "2026-10-31T17:32:56Z error: successful probe\n"
    '2026-10-31T17:32:57Z ##[group]Run echo "error: not output"\n'
    '2026-10-31T17:32:57Z echo "error: not output"\n'
    "2026-10-31T17:32:57Z ##[endgroup]\n"
    "2026-10-31T17:33:04.2276296Z ##[error]Appstream: releases-not-in-order\n"
    "2026-10-31T17:33:04.3453806Z error: recipe `validate-build` failed with exit code 1\n"
    "2026-10-31T17:33:04.4218950Z ##[error]Process completed with exit code 1.\n"
    "2026-10-31T17:33:04.4281717Z error: cleanup in same second"
)


def test_failed_step_filter_excludes_success_and_same_second_cleanup():
    result = extract_diagnostic(failed_step_log(LOG, STEPS))
    assert result.module is None
    assert result.excerpt == "##[error]Appstream: releases-not-in-order"
    assert failed_step_log(LOG, []) == ""


def test_failed_step_excludes_previous_output_in_shared_start_second():
    log = LOG.replace(
        "2026-10-31T17:32:56Z error: successful probe",
        "2026-10-31T17:32:57Z error: successful probe",
    )
    result = extract_diagnostic(failed_step_log(log, STEPS))
    assert result.excerpt == "##[error]Appstream: releases-not-in-order"


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        (
            "2026-10-31T17:32:57Z ##[group]Run previous-step\n"
            "2026-10-31T17:32:57Z ##[endgroup]\n"
            "2026-10-31T17:32:57Z error: previous-step\n"
            "2026-10-31T17:32:57Z ##[group]Run failed-step\n"
            "2026-10-31T17:32:57Z ##[endgroup]\n"
        ),
    ],
)
def test_missing_or_ambiguous_step_boundary_has_no_excerpt(prefix):
    log = prefix + "2026-10-31T17:33:03Z error: cannot attribute this safely"
    assert failed_step_log(log, STEPS) == ""


def test_next_step_boundary_stops_excerpt_without_exit_annotation():
    log = (
        "2026-10-31T17:32:57Z ##[group]Run failed-step\n"
        "2026-10-31T17:32:57Z ##[endgroup]\n"
        "2026-10-31T17:33:03Z error: actual failure\n"
        "2026-10-31T17:33:04Z ##[group]Run cleanup\n"
        "2026-10-31T17:33:04Z ##[endgroup]\n"
        "2026-10-31T17:33:04Z error: cleanup failure"
    )
    assert failed_step_log(log, STEPS) == "error: actual failure"


@pytest.mark.asyncio
async def test_separate_failed_jobs_and_missing_log_fallback():
    client = Mock(
        request=AsyncMock(
            side_effect=[
                Mock(
                    json=lambda: {
                        "jobs": [
                            {
                                "id": 2,
                                "name": "build-x86_64",
                                "conclusion": "failure",
                                "steps": STEPS,
                            },
                            {
                                "id": 1,
                                "name": "build-aarch64",
                                "conclusion": "failure",
                                "steps": STEPS,
                            },
                            {"id": 3, "name": "callback", "conclusion": "failure"},
                            {"id": 4, "name": "build-other", "conclusion": "cancelled"},
                        ]
                    }
                ),
                Mock(text=LOG),
                None,
            ]
        )
    )
    with patch(
        "app.services.build_failure.get_github_actions_client", return_value=client
    ):
        result = await build_failure_summary(
            "https://github.com/flathub-infra/vorarbeiter/actions/runs/123"
        )
    assert result.index("build-aarch64") < result.index("build-x86_64")
    assert "releases-not-in-order" in result
    assert "No specific diagnostic" in result
    assert "/job/1" in result and "/job/2" in result
    assert "callback" not in result and "build-other" not in result
    assert client.request.await_count == 3


@pytest.mark.asyncio
async def test_api_failure_keeps_honest_fallback():
    with patch("app.services.build_failure.get_github_actions_client") as factory:
        factory.return_value.request = AsyncMock(
            side_effect=RuntimeError("unavailable")
        )
        result = await build_failure_summary(
            "https://github.com/flathub-infra/vorarbeiter/actions/runs/123"
        )
    assert result == "Diagnostic excerpt unavailable; see the full build log."
