import re
from dataclasses import dataclass


@dataclass
class MergeCommand:
    target_branch: str
    pr_head_sha: str
    additional_collaborators: list[str]
    branch_explicit: bool


@dataclass
class AppMergeCommand:
    pr_head_sha: str
    merge_method: str | None
    force: bool


_BRANCH_PATTERN = re.compile(r"[\w.-]+")
_SHA_PATTERN = re.compile(r"[a-fA-F0-9]{40}")
_USERNAME_PATTERN = re.compile(r"@(?!flathub/)[a-zA-Z0-9-]{1,39}")
_TEAM_PATTERN = re.compile(r"@flathub/[a-zA-Z0-9-]{1,39}")
_METHOD_PATTERN = re.compile(r"merge|squash|rebase")
_FORCE_PATTERN = re.compile(r"true|false")


def _split_args(
    comment: str, keys: set[str]
) -> tuple[dict[str, str], list[str]] | None:
    comment = comment.rstrip()
    if "\n" in comment:
        return None
    tokens = comment.split()
    if not tokens or tokens[0] != "/merge":
        return None
    args: dict[str, str] = {}
    rest: list[str] = []
    for token in tokens[1:]:
        key, sep, value = token.partition("=")
        if sep and key in keys:
            if key in args:
                return None
            args[key] = value
        else:
            rest.append(token)
    return args, rest


def parse_merge_command(comment: str) -> MergeCommand | None:
    split = _split_args(comment, {"branch", "head"})
    if not split:
        return None
    args, rest = split
    if any("=" in token for token in rest):
        return None

    pr_head_sha = args.get("head")
    if not pr_head_sha or not _SHA_PATTERN.fullmatch(pr_head_sha):
        return None

    branch_match = args.get("branch")
    if branch_match is not None and not _BRANCH_PATTERN.fullmatch(branch_match):
        return None
    branch_explicit = branch_match is not None
    if not branch_match:
        target_branch = "master"
    elif branch_match in ("master", "beta"):
        target_branch = branch_match
    else:
        target_branch = f"branch/{branch_match}"

    rest_text = " ".join(rest)
    collaborators = [m[1:] for m in _USERNAME_PATTERN.findall(rest_text)]
    collaborators.extend(m[1:] for m in _TEAM_PATTERN.findall(rest_text))

    return MergeCommand(
        target_branch=target_branch,
        pr_head_sha=pr_head_sha,
        additional_collaborators=collaborators,
        branch_explicit=branch_explicit,
    )


def parse_app_merge_command(comment: str) -> AppMergeCommand | None:
    split = _split_args(comment, {"method", "force", "head"})
    if not split:
        return None
    args, rest = split
    if rest:
        return None

    pr_head_sha = args.get("head")
    if not pr_head_sha or not _SHA_PATTERN.fullmatch(pr_head_sha):
        return None
    merge_method = args.get("method")
    if merge_method is not None and not _METHOD_PATTERN.fullmatch(merge_method):
        return None
    force = args.get("force")
    if force is not None and not _FORCE_PATTERN.fullmatch(force):
        return None

    return AppMergeCommand(
        pr_head_sha=pr_head_sha,
        merge_method=merge_method,
        force=force == "true",
    )
