"""GitHub Actions log analysis utilities for detecting spot instance cancellations."""

import re


def _preprocess_log_content(log_content: str) -> list[str]:
    """Preprocess log content by splitting into lines and focusing on the tail."""
    if not log_content:
        return []

    lines = log_content.strip().split("\n")
    return lines[-20:] if len(lines) > 20 else lines


def _match_cancellation_pattern(line: str) -> bool:
    """Check if a line contains cancellation patterns."""
    line_lower = line.lower()

    if "##[error]" not in line_lower:
        return False

    cancellation_patterns = [
        r"error:\s*the\s+operation\s+was\s+canceled",
        r"the\s+runner\s+has\s+received\s+a\s+shutdown\s+signal",
        r"the\s+job\s+was\s+canceled",
        r"received\s+shutdown\s+signal",
        r"operation\s+was\s+cancelled",
    ]

    for pattern in cancellation_patterns:
        if re.search(pattern, line_lower):
            return True

    return False


def was_spot_cancelled(log_content: str) -> bool:
    """
    Analyze GitHub Actions log content to determine if the build was cancelled
    due to spot instance termination.

    Args:
        log_content: The raw log content from GitHub Actions

    Returns:
        True if cancellation patterns are detected, False otherwise
    """
    if not log_content or not isinstance(log_content, str):
        return False

    lines = _preprocess_log_content(log_content)

    for line in lines:
        if _match_cancellation_pattern(line):
            return True

    return False
