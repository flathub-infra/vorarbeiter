import re
from dataclasses import dataclass


@dataclass
class MergeCommand:
    target_branch: str
    pr_head_sha: str
    additional_collaborators: list[str]


_COMMAND_PATTERN = re.compile(r"^/merge(?::([\w.-]+))? head=([a-fA-F0-9]{40})(.*)$")

_USERNAME_PATTERN = re.compile(r"@(?!flathub/)[a-zA-Z0-9-]{1,39}")
_TEAM_PATTERN = re.compile(r"@flathub/[a-zA-Z0-9-]{1,39}")


def parse_merge_command(comment: str) -> MergeCommand | None:
    if not comment.startswith("/merge"):
        return None

    matched = _COMMAND_PATTERN.search(comment)
    if not matched:
        return None

    branch_match = matched.group(1) or "master"
    if branch_match in ("master", "beta"):
        target_branch = branch_match
    else:
        target_branch = f"branch/{branch_match}"

    pr_head_sha = matched.group(2)

    rest = matched.group(3)
    collaborators = [m[1:] for m in _USERNAME_PATTERN.findall(rest)]
    collaborators.extend(m[1:] for m in _TEAM_PATTERN.findall(rest))

    return MergeCommand(
        target_branch=target_branch,
        pr_head_sha=pr_head_sha,
        additional_collaborators=collaborators,
    )
