"""Tests for GitHub Actions log analysis utilities."""

import pytest

from app.utils.github_actions_logs import (
    was_spot_cancelled,
    _preprocess_log_content,
    _match_cancellation_pattern,
)


class TestGitHubActionsLogs:
    """Test GitHub Actions log analysis functionality."""

    def test_was_spot_cancelled_with_cancellation_pattern(self):
        """Test detection of spot instance cancellation from logs."""
        log_content = """
        2024-01-01T10:00:00.000Z Starting build...
        2024-01-01T10:01:00.000Z Running tests...
        2024-01-01T10:02:00.000Z Building application...
        2024-01-01T10:03:00.000Z ##[error]Error: The operation was canceled.
        2024-01-01T10:03:01.000Z Cleaning up...
        """

        assert was_spot_cancelled(log_content) is True

    def test_was_spot_cancelled_with_shutdown_signal(self):
        """Test detection of shutdown signal cancellation."""
        log_content = """
        2024-01-01T10:00:00.000Z Starting build...
        2024-01-01T10:01:00.000Z Running tests...
        2024-01-01T10:02:00.000Z ##[error]The runner has received a shutdown signal. This can happen when the runner service is stopped, or a manually started runner is canceled.
        """

        assert was_spot_cancelled(log_content) is True

    def test_was_spot_cancelled_with_job_canceled(self):
        """Test detection of job cancellation."""
        log_content = """
        2024-01-01T10:00:00.000Z Starting build...
        2024-01-01T10:01:00.000Z ##[error]The job was canceled
        """

        assert was_spot_cancelled(log_content) is True

    def test_was_spot_cancelled_with_british_spelling(self):
        """Test detection with British spelling of cancelled."""
        log_content = """
        2024-01-01T10:00:00.000Z Starting build...
        2024-01-01T10:01:00.000Z ##[error]Error: The operation was cancelled.
        """

        assert was_spot_cancelled(log_content) is True

    def test_was_spot_cancelled_case_insensitive(self):
        """Test that pattern matching is case insensitive."""
        log_content = """
        2024-01-01T10:00:00.000Z Starting build...
        2024-01-01T10:01:00.000Z ##[ERROR]ERROR: THE OPERATION WAS CANCELED.
        """

        assert was_spot_cancelled(log_content) is True

    def test_was_spot_cancelled_with_build_failure(self):
        """Test that actual build failures are not detected as cancellations."""
        log_content = """
        2024-01-01T10:00:00.000Z Starting build...
        2024-01-01T10:01:00.000Z Running tests...
        2024-01-01T10:02:00.000Z ##[error]Build failed: compilation error
        2024-01-01T10:02:01.000Z ##[error]undefined reference to 'missing_function'
        2024-01-01T10:02:02.000Z Process completed with exit code 1
        """

        assert was_spot_cancelled(log_content) is False

    def test_was_spot_cancelled_with_empty_log(self):
        """Test handling of empty log content."""
        assert was_spot_cancelled("") is False
        assert was_spot_cancelled(None) is False  # type: ignore[arg-type]

    def test_was_spot_cancelled_with_non_string_input(self):
        """Test handling of non-string input."""
        assert was_spot_cancelled(123) is False  # type: ignore[arg-type]
        assert was_spot_cancelled([]) is False  # type: ignore[arg-type]
        assert was_spot_cancelled({}) is False  # type: ignore[arg-type]

    def test_was_spot_cancelled_without_error_marker(self):
        """Test that cancellation patterns without ##[error] are not detected."""
        log_content = """
        2024-01-01T10:00:00.000Z Starting build...
        2024-01-01T10:01:00.000Z Error: The operation was canceled.
        2024-01-01T10:02:00.000Z Done
        """

        assert was_spot_cancelled(log_content) is False

    def test_was_spot_cancelled_focuses_on_tail(self):
        """Test that the function focuses on the last lines of large logs."""
        # Create a log with cancellation pattern at the beginning and end
        lines = [f"2024-01-01T10:00:{i:02d}.000Z Line {i}" for i in range(50)]
        lines[5] = (
            "2024-01-01T10:00:05.000Z ##[error]Error: The operation was canceled."
        )
        lines[45] = "2024-01-01T10:00:45.000Z ##[error]Build failed: compilation error"

        log_content = "\n".join(lines)

        # Should not detect cancellation because the pattern at the beginning
        # is outside the tail window, and the tail has a build failure
        assert was_spot_cancelled(log_content) is False

    def test_preprocess_log_content_empty(self):
        """Test preprocessing of empty log content."""
        assert _preprocess_log_content("") == []
        assert _preprocess_log_content(None) == []  # type: ignore[arg-type]

    def test_preprocess_log_content_short_log(self):
        """Test preprocessing of short log content."""
        log_content = "line1\nline2\nline3"
        result = _preprocess_log_content(log_content)
        assert result == ["line1", "line2", "line3"]

    def test_preprocess_log_content_long_log(self):
        """Test preprocessing of long log content focuses on tail."""
        lines = [f"line{i}" for i in range(30)]
        log_content = "\n".join(lines)
        result = _preprocess_log_content(log_content)

        # Should return last 20 lines
        assert len(result) == 20
        assert result[0] == "line10"
        assert result[-1] == "line29"

    def test_match_cancellation_pattern_with_error_marker(self):
        """Test pattern matching with error marker."""
        assert (
            _match_cancellation_pattern("##[error]Error: The operation was canceled.")
            is True
        )
        assert (
            _match_cancellation_pattern("##[ERROR]ERROR: THE OPERATION WAS CANCELED")
            is True
        )

    def test_match_cancellation_pattern_without_error_marker(self):
        """Test pattern matching without error marker."""
        assert (
            _match_cancellation_pattern("Error: The operation was canceled.") is False
        )
        assert _match_cancellation_pattern("The operation was canceled.") is False

    def test_match_cancellation_pattern_various_patterns(self):
        """Test pattern matching with various cancellation patterns."""
        patterns = [
            "##[error]Error: The operation was canceled.",
            "##[error]The runner has received a shutdown signal",
            "##[error]The job was canceled",
            "##[error]Received shutdown signal",
            "##[error]Operation was cancelled",  # British spelling
        ]

        for pattern in patterns:
            assert _match_cancellation_pattern(pattern) is True, (
                f"Failed for pattern: {pattern}"
            )

    def test_match_cancellation_pattern_non_cancellation(self):
        """Test pattern matching with non-cancellation error messages."""
        patterns = [
            "##[error]Build failed: compilation error",
            "##[error]Test failed",
            "##[error]undefined reference to function",
            "##[error]File not found",
        ]

        for pattern in patterns:
            assert _match_cancellation_pattern(pattern) is False, (
                f"False positive for pattern: {pattern}"
            )

    @pytest.mark.parametrize(
        "log_content,expected",
        [
            # Real-world-like cancellation logs
            (
                """
2024-01-15T14:30:25.1234567Z ##[group]Run actions/checkout@v4
2024-01-15T14:30:25.1234567Z with:
2024-01-15T14:30:45.1234567Z ##[endgroup]
2024-01-15T14:35:12.1234567Z ##[group]Setup Python
2024-01-15T14:35:30.1234567Z ##[endgroup]
2024-01-15T14:40:15.1234567Z ##[error]Error: The operation was canceled.
2024-01-15T14:40:15.1234567Z ##[group]Post job cleanup
        """,
                True,
            ),
            # Real-world-like build failure logs
            (
                """
2024-01-15T14:30:25.1234567Z ##[group]Run actions/checkout@v4
2024-01-15T14:30:45.1234567Z ##[endgroup]
2024-01-15T14:35:12.1234567Z ##[group]Build Application
2024-01-15T14:35:30.1234567Z Building...
2024-01-15T14:40:15.1234567Z ##[error]gcc: error: undefined reference to 'main'
2024-01-15T14:40:16.1234567Z ##[error]Process completed with exit code 1.
        """,
                False,
            ),
            # Empty or minimal logs
            ("", False),
            ("2024-01-15T14:30:25.1234567Z Starting...", False),
        ],
    )
    def test_was_spot_cancelled_realistic_scenarios(self, log_content, expected):
        """Test spot cancellation detection with realistic log scenarios."""
        assert was_spot_cancelled(log_content) is expected
