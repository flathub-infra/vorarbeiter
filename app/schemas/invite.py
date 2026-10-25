import re
from dataclasses import dataclass


@dataclass
class InviteCommand:
    repo: str
    users: list[str]


_COMMAND_PATTERN = re.compile(
    r"^/invite repo=(?:flathub/)?([\w.-]+)((?:\s+@[a-zA-Z0-9-]{1,39})+)\s*$"
)


def parse_invite_command(comment: str) -> InviteCommand | None:
    lines = comment.splitlines()
    if not lines:
        return None

    matched = _COMMAND_PATTERN.match(lines[0].strip())
    if not matched:
        return None

    users: dict[str, str] = {}
    for mention in matched.group(2).split():
        users.setdefault(mention[1:].lower(), mention[1:])

    return InviteCommand(repo=matched.group(1), users=list(users.values()))
