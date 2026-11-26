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
                r"|\.credentials\.json\b|(?<![\w.-])auth\.json\b|\.git-credentials\b|\.netrc\b"
                r"|\.aws/credentials\b|\.kaptyorka/config\.json\b|\.claude/\.credentials", I)),
    ("a request for a token or a password",
     re.compile(r"\bCLAUDE_CODE_OAUTH_TOKEN\b"
                r"|\b(?:printenv|env)\b" + S + r"[^\n]*\b\w*(?:token|secret|api_?key|password)\w*"
                r"|\becho" + S + r"[\"']?\$\{?\w*(?:TOKEN|SECRET|API_?KEY|PASSWORD)"
                r"|\b" + GIVE + r"[^\n.;]{0,60}?\b(?:your|the|its|his|her|their|my|all)" + S +
                r"(?:\w+" + S + r"){0,2}?(?:api" + S + r"?keys?|tokens?|passwords?|passphrases?|oauth|credentials|secrets?|sign-?in" + S + r"file|login" + S + r"file)\b"
                r"(?!" + S + r"(?:count|budget|usage|limit|length|size|cost)s?\b)", I)),
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
