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


# A value someone gives as a secret in plain words: right after its label and "is", ":" or
# "=" ("the password is Xy7kd92mQ", "token: ab12cd34ef", "api key = ..."). No pattern knows a
# password, so the manager's writes are checked against the values people sent it this way
# (hub.py, manager_secret). A label further away ("lost its login, please resume w-12") gives
# nothing: names after it are ordinary.
LABELLED = re.compile(r"\b(?:password|passwd|passphrase|pwd|secret|token|api[ _-]?key"
                      r"|access[ _-]?key|private[ _-]?key|credentials?|login)"
                      r"(?:\s+\S+){0,3}?(?:\s+is\b\s*[:=]?|\s*[:=])\s*[`'\"]?(\S+)", re.I)


def labelled_values(text: str) -> set:
    """Values given right after a secret's label: 8 or more characters with a letter and a
    digit (so plain words never count), no path or address."""
    out = set()
    for m in LABELLED.finditer(text or ""):
        word = m.group(1).strip("`'\".,;:()[]<>")
        if len(word) >= 8 and re.search(r"[A-Za-z]", word) and re.search(r"\d", word) \
                and "/" not in word and ":" not in word:
            out.add(word)
    return out
