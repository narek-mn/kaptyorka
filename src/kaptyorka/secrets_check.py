import re

START = r"(?<![A-Za-z0-9_-])"  # not inside a longer token, which may contain "sk-" by chance

PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenAI-style key", re.compile(START + r"sk-(proj-|ant-[a-z0-9]+-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(START + r"gh[oprsu]_[A-Za-z0-9]{30,}")),
    ("GitHub token", re.compile(START + r"github_pat_[A-Za-z0-9_]{20,}")),
    ("GitLab token", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Slack token", re.compile(r"xox[bap]-[A-Za-z0-9-]{20,}")),
]


TEXT_TYPES = ("txt", "md", "log", "json", "csv")


def check_secrets(text: str) -> None:
    from kaptyorka.store import KaptyorkaError

    for what, pattern in PATTERNS:
        if pattern.search(text):
            raise KaptyorkaError(f"looks like a secret: {what}")
