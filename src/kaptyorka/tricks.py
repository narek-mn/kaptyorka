"""The hub's trick filter: plain patterns, no AI. A message whose text matches one is not
delivered but held for an admin (Store.send), who releases or drops it. Every pattern has a
short reason, shown to the sender and to the admin. The list is meant to be short and
specific: ordinary team talk ("run the tests", "git status", "chmod +x run.sh") never
matches. It lowers how often a chat is tricked; it is not a wall."""
import re
from typing import List, Optional

I = re.IGNORECASE
S = r"\s+"  # any run of spaces, tabs or line breaks
SHELL = r"(?:sudo\s+(?:-\S+\s+)?)?(?:ba|z|da|k|c|fi)?sh\b|python3?\b|perl\b|node\b|iex\b|invoke-expression\b"
FETCH = r"(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b"
HOME = r"(?:~|\$\{?HOME\}?|/home/[\w.-]+|/Users/[\w.-]+|%USERPROFILE%|\$env:USERPROFILE)"
GIVE = r"(?:cat|print|show|send|upload|post|paste|read|dump|echo|email|mail|attach|scp|tar|zip|curl|give|share|reveal|tell|copy|cp|forward|leak)\b"
# an order at the start of a clause ("print auth.json", "then send me ..."), not a description
ORDER = r"(?:^|[.;:!?,\n(]\s*|\b(?:please|then|and|now|just|also|can\s+you|could\s+you|pls)\s+)"
# what a chat must never hand over. A bare "token" is everyday ML talk (the first 10 tokens, the
# tokenizer, tokens per second), so it counts only after an auth word, or as someone's own
# ("your token"); "password" and "credentials" only as the secret, not a page or a field.
AUTH = r"(?:oauth|access|refresh|api|bearer|setup|session|secret|auth|personal" + S + r"access|github|gitlab|hf|npm|pypi|claude|codex|cursor|opencode)"
SECRET = (r"api" + S + r"?keys?|" + AUTH + r"(?:" + S + r"|[_-])tokens?|(?<=your" + r"\s)tokens?|(?<=my\s)tokens?"
          r"|passwords?|passphrases?|oauth|credentials|secrets?|sign-?in" + S + r"file|login" + S + r"file")
NOT_SECRET = (r"(?:count|budget|usage|limit|length|size|cost|ids?|per|rate|throughput|embeddings?|reset|field|page|form"
              r"|policy|policies|input|box|strength|hash(?:ing)?|rules?|validation|flow|route|screen|ui|component|tests?"
              r"|check|prompt|change|requirements?|manager|rotation|expiry|refresh|logic|handling|schema|error|dialog"
              r"|label|modal|hint|step|section|docs?|parser|format)s?")

# (reason, pattern) in the order they are reported
PATTERNS = [
    ("a git setting that runs a program",
     re.compile(r"\bcore\s*\.\s*(?:fsmonitor|hooks?path|sshcommand|pager|editor|gitproxy|askpass)\b"
                r"|\b(?:diff|merge)\s*\.\s*external\b"
                r"|\bdiff\s*\.\s*[\w-]+\s*\.\s*(?:textconv|command)\b"
                r"|\bmerge\s*\.\s*[\w-]+\s*\.\s*driver\b"
                r"|\bfilter\s*\.\s*[\w.-]+\s*\.\s*(?:clean|smudge|process)\b"
                r"|\balias\s*\.\s*[\w-]+\s*=?\s*['\"]?!"
                r"|\bgit" + S + r"(?:-c" + S + r")?config\b[^\n]*\b(?:core\.(?:fsmonitor|hooks?path|sshcommand|pager|editor)"
                r"|diff\.external)", I)),
    ("a command that downloads and runs code",
     re.compile(FETCH + r"[^\n]*?\|" + r"\s*(?:" + SHELL + r")"
                r"|\b(?:ba|z|da|k)?sh" + S + r"<\(\s*" + FETCH +
                r"|\b(?:source|\.)" + S + r"<\(\s*" + FETCH +
                r"|\b(?:ba|z|da|k)?sh" + S + r"-c" + S + r"[\"']?\$\(\s*" + FETCH +
                r"|\b(?:eval|iex|invoke-expression)\b[^\n]*" + FETCH, I)),
    ("a downloaded file made executable",
     re.compile(FETCH + r"[\s\S]{0,300}?\bchmod" + S + r"(?:-\w+" + S + r")?(?:[ugoa]*\+[rw]*x|[0-7]?[0-7][5-7][0-7])\b"
                r"|\bchmod" + S + r"(?:-\w+" + S + r")?(?:[ugoa]*\+[rw]*x|[0-7]?[0-7][5-7][0-7])\b[\s\S]{0,300}?" + FETCH, I)),
    ("hidden or encoded shell code",
     re.compile(r"\bbase64" + S + r"(?:-d\b|-D\b|--decode\b)[^\n]*\|\s*(?:" + SHELL + r"|eval\b)"
                r"|\b(?:eval|source|\.|(?:ba|z)?sh)\b[^\n]*(?:\$\(|<\(|`)\s*(?:echo|printf|cat)\b[^\n)`]*\|\s*base64"
                r"|\bpowershell\b[^\n]*\s-e(?:nc|ncodedcommand)?\s+[A-Za-z0-9+/=]{20,}"
                r"|\bFromBase64String\b[^\n]*\b(?:iex|invoke-expression)\b"
                r"|\bxxd" + S + r"-r\b[^\n]*\|\s*(?:" + SHELL + r")", I)),
    ("direnv or .envrc, which run commands when a folder is entered",
     re.compile(r"(?<![\w-])\.envrc\b|\bdirenv" + S + r"(?:allow|export|exec)\b", I)),
    ("an editor task that runs when a folder is opened",
     re.compile(r"\.vscode/tasks\.json|\brunOn[\"']?\s*:\s*[\"']?folderOpen"
                r"|\btask\.allowAutomaticTasks\b"
                r"|\.vscode/settings\.json[^\n]*\b(?:terminal\.integrated\.(?:automationProfile|profiles|defaultProfile)|git\.path)\b", I)),
    ("a private key",
     re.compile(r"BEGIN" + S + r"(?:[A-Z]+" + S + r")*PRIVATE" + S + r"KEY|PuTTY-User-Key-File", I)),
    ("a request for private files",
     re.compile(r"(?<![\w.-])" + HOME + r"/\.ssh\b|(?<![\w.-])\.ssh/(?:id_\w+|authorized_keys|config)\b"
                r"|\bid_(?:rsa|ed25519|ecdsa|dsa)\b(?!\.pub)"
                r"|\.credentials\.json\b|\.git-credentials\b|\.netrc\b"
                # auth.json is also a name in ordinary code ("the login route reads auth.json"):
                # only a tool's sign-in folder, or an order to hand the file over
                r"|(?:\.?codex|opencode|\.?cursor|\.config/[\w.-]+|\.local/share/[\w.-]+)/auth\.json\b"
                r"|" + ORDER + GIVE + r"\s+(?:(?:me|us)\s+)?(?:(?:the|your|its)\s+)?(?:contents?\s+of\s+)?"
                r"(?:[\w~$.-]*/)*auth\.json\b"
                r"|\.aws/credentials\b|\.kaptyorka/config\.json\b|\.claude/\.credentials", I)),
    ("a request for a token or a password",
     re.compile(r"\bCLAUDE_CODE_OAUTH_TOKEN\b"
                # an environment name that holds one (GITHUB_TOKEN, MY_SECRET), not MAX_NEW_TOKENS=256
                r"|\b(?:printenv|env)\b" + S + r"(?:\|\s*grep" + S + r"(?:-\w+" + S + r")*)?[\"']?"
                r"\w*(?:token|secret|api_?key|password)(?![\w=])"
                r"|\becho" + S + r"[\"']?\$\{?\w*(?:TOKEN|SECRET|API_?KEY|PASSWORD)(?!\w)"
                r"|\b" + GIVE + r"[^\n.;]{0,60}?\b(?:your|the|its|his|her|their|my|all)" + S +
                r"(?:\w+" + S + r"){0,2}?(?:" + SECRET + r")\b(?!" + S + NOT_SECRET + r"\b)", I)),
]

# Hidden text: characters a person cannot see. A joiner between emoji (ZWJ) or a half space
# inside Arabic-script words (ZWNJ) is ordinary and is not flagged.
HIDDEN = re.compile("[​⁠﻿᠎⁡-⁤‪-‮⁦-⁩"
                    "\U000e0000-\U000e007f\x1b\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]"
                    "|(?<=[\x00-\x7f])‍|‍(?=[\x00-\x7f])"
                    "|(?<![؀-ࣿ])‌|‌(?![؀-ࣿ])")
HIDDEN_REASON = "hidden characters in the text (zero-width, direction override, tag or control characters)"


def reasons(text: str) -> List[str]:
    """Every reason the text is held, each once, in list order; [] for ordinary text."""
    text = text or ""
    out = []
    if HIDDEN.search(text):
        out.append(HIDDEN_REASON)
    flat = HIDDEN.sub("", text)  # so a hidden character cannot split a word to dodge a pattern
    for reason, pattern in PATTERNS:
        if reason not in out and pattern.search(flat):
            out.append(reason)
    return out


def check_tricks(text: str) -> Optional[str]:
    """One plain reason to hold the text, or None."""
    found = reasons(text)
    return "; ".join(found) if found else None


def visible(text: str) -> str:
    """The text with each hidden character shown as <U+XXXX>, for the admin who judges it."""
    return HIDDEN.sub(lambda m: f"<U+{ord(m.group()):04X}>", text or "")
