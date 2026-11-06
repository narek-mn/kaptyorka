import re

PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenAI-style key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("GitHub token", re.compile(r"ghp_[A-Za-z0-9]{30,}")),
    ("GitLab token", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Slack token", re.compile(r"xox[bap]-[A-Za-z0-9-]{20,}")),
]


def check_secrets(text: str) -> None:
    from kaptyorka.store import KaptyorkaError

    for what, pattern in PATTERNS:
        if pattern.search(text):
            raise KaptyorkaError(f"looks like a secret: {what}")
