"""Fail if personal data would be published.

Scans every tracked file, plus the author, committer and message of every
commit reachable from a revision (default HEAD). Exits non-zero on:

* real email addresses (GitHub noreply and the Anthropic noreply are allowed)
* home-folder paths under the Windows or macOS user directory
* any term listed in the git-ignored ``.private-terms`` file (one per line,
  ``#`` starts a comment, matching is case-insensitive)
* tracked files under ``docs/`` or in a folder named ``superpowers`` or
  ``.superpowers``
* token-like strings

Findings name the location and kind only, never the matched text.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_USERS = "Users"
EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
HOME_PATH_RE = re.compile(
    rf"[A-Za-z]:[\\/]+{_USERS}[\\/]|(?<![A-Za-z0-9_.])/{_USERS}/", re.IGNORECASE
)
TOKEN_RES = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
ALLOWED_EMAILS = {"noreply@anthropic.com"}
ALLOWED_EMAIL_DOMAINS = {"users.noreply.github.com"}
FORBIDDEN_DIRS = {"superpowers", ".superpowers"}
MAX_FILE_BYTES = 5 * 1024 * 1024


def load_terms(root: Path) -> list[str]:
    path = Path(root) / ".private-terms"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    terms = [ln.strip() for ln in lines]
    return [t for t in terms if t and not t.startswith("#")]


def _email_allowed(email: str) -> bool:
    email = email.lower()
    return email in ALLOWED_EMAILS or email.rsplit("@", 1)[-1] in ALLOWED_EMAIL_DOMAINS


def scan_text(text: str, label: str, terms: list[str]) -> list[str]:
    found: list[str] = []
    for m in EMAIL_RE.finditer(text):
        if not _email_allowed(m.group(0)):
            found.append(f"{label}: email address")
    if HOME_PATH_RE.search(text):
        found.append(f"{label}: home-folder path")
    for rx in TOKEN_RES:
        if rx.search(text):
            found.append(f"{label}: token-like string")
    lowered = text.lower()
    for i, term in enumerate(terms, 1):
        if term.lower() in lowered:
            found.append(f"{label}: private term #{i}")
    return found


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False
    )


def _path_problem(rel: str) -> str | None:
    parts = rel.replace("\\", "/").split("/")
    if parts[0] == "docs":
        return "tracked file under docs/"
    if any(p in FORBIDDEN_DIRS for p in parts[:-1]):
        return "tracked file in a superpowers folder"
    return None


def check_tree(root: Path, terms: list[str]) -> list[str]:
    found: list[str] = []
    out = _git(root, "ls-files", "-z").stdout.decode("utf-8", "replace")
    for rel in filter(None, out.split("\0")):
        problem = _path_problem(rel)
        if problem:
            found.append(f"{rel}: {problem}")
        found.extend(scan_text(rel, f"{rel} (file name)", terms))
        path = Path(root) / rel
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue
        found.extend(scan_text(data.decode("utf-8", "replace"), rel, terms))
    return found


def check_commits(root: Path, terms: list[str], rev: str = "HEAD") -> list[str]:
    fmt = "%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e"
    res = _git(root, "log", f"--format={fmt}", rev, "--")
    if res.returncode != 0:  # no commits yet
        return []
    found: list[str] = []
    for record in res.stdout.decode("utf-8", "replace").split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, an, ae, cn, ce, body = (record.split("\x1f", 5) + [""] * 6)[:6]
        short = sha[:8]
        for role, name, email in (("author", an, ae), ("committer", cn, ce)):
            found.extend(scan_text(f"{name} <{email}>", f"commit {short} {role}", terms))
        found.extend(scan_text(body, f"commit {short} message", terms))
    return found


def check_repo(root: Path, rev: str = "HEAD") -> list[str]:
    root = Path(root)
    terms = load_terms(root)
    return check_tree(root, terms) + check_commits(root, terms, rev)


def main(argv: list[str]) -> int:
    top = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print("not inside a git repository", file=sys.stderr)
        return 2
    root = Path(top.stdout.decode("utf-8").strip())
    rev = argv[1] if len(argv) > 1 else "HEAD"
    problems = check_repo(root, rev)
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(f"private data check FAILED ({len(problems)} finding(s))", file=sys.stderr)
        return 1
    print("private data check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
