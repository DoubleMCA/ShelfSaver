"""Tests for scripts/check_private_data.py.

Sensitive-looking literals are assembled at runtime so this file itself
stays clean when the check scans the repository.
"""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_private_data",
    Path(__file__).resolve().parent.parent / "scripts" / "check_private_data.py",
)
cpd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cpd)

AT = "@"
PERSONAL = "someone" + AT + "example.org"
NOREPLY = "12345+someone" + AT + "users.noreply.github.com"
ANTHROPIC = "noreply" + AT + "anthropic.com"
HOME_WIN = "C:" + "\\" + "Users" + "\\" + "somebody" + "\\" + "Videos"
HOME_MAC = "/" + "Users" + "/" + "somebody"


def git(repo, *args, env=None):
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=full_env
    )


def commit(repo, message="feat: thing", email=NOREPLY, name="Someone"):
    env = {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
    }
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull,
        "commit", "-m", message, env=env)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "ok.txt").write_text("nothing private here\n", encoding="utf-8")
    commit(tmp_path)
    return tmp_path


def kinds(findings):
    return " | ".join(findings)


def test_flags_email(repo):
    (repo / "notes.txt").write_text("contact " + PERSONAL + "\n", encoding="utf-8")
    commit(repo)
    assert "email address" in kinds(cpd.check_repo(repo))


def test_flags_home_path(repo):
    (repo / "a.txt").write_text(HOME_WIN + "\n", encoding="utf-8")
    (repo / "b.txt").write_text("see " + HOME_MAC + "/x\n", encoding="utf-8")
    commit(repo)
    found = cpd.check_repo(repo)
    assert any(f.startswith("a.txt") and "home-folder path" in f for f in found)
    assert any(f.startswith("b.txt") and "home-folder path" in f for f in found)


def test_flags_denylist_term(repo):
    (repo / ".private-terms").write_text("# comment\nMyBox-Tower\n", encoding="utf-8")
    (repo / "a.txt").write_text("host is mybox-tower today\n", encoding="utf-8")
    commit(repo)
    found = cpd.check_repo(repo)
    assert "private term #1" in kinds(found)
    assert "mybox" not in kinds(found).lower()  # never echoes the term


def test_flags_personal_commit_author(repo):
    (repo / "b.txt").write_text("fine\n", encoding="utf-8")
    commit(repo, email=PERSONAL)
    assert "author" in kinds(cpd.check_repo(repo))


def test_allows_noreply(repo):
    (repo / "c.txt").write_text("fine\n", encoding="utf-8")
    message = (
        "feat: more\n\nCo-Authored-By: Claude <" + ANTHROPIC + ">\n"
        "Claude-Session: https://claude.ai/code/session_abc123\n"
    )
    commit(repo, message=message, email=NOREPLY)
    assert cpd.check_repo(repo) == []


def test_clean_tree_passes(repo):
    assert cpd.check_repo(repo) == []


def test_flags_docs_and_superpowers_paths(repo):
    for rel in ("docs/plan.md", "x/superpowers/n.md", ".superpowers/n.md"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("plain\n", encoding="utf-8")
    git(repo, "add", "-f", "-A")
    commit(repo)
    found = [f for f in cpd.check_repo(repo) if "tracked file" in f]
    assert len(found) == 3


def test_flags_token(repo):
    (repo / "t.txt").write_text("key=" + "ghp_" + "a" * 36 + "\n", encoding="utf-8")
    commit(repo)
    assert "token-like string" in kinds(cpd.check_repo(repo))


def test_flags_personal_committer_in_message_history(repo):
    (repo / "d.txt").write_text("fine\n", encoding="utf-8")
    commit(repo, message="note for " + PERSONAL)
    (repo / "e.txt").write_text("fine\n", encoding="utf-8")
    commit(repo)  # later clean commit does not hide the bad one
    assert "message" in kinds(cpd.check_repo(repo))
