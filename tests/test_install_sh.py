"""The Linux installer's helpers. Only pieces of the script run here: as a whole it needs root
and changes the machine, so the full install is checked end-to-end by hand (see the README)."""
from __future__ import annotations

import getpass
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bash") is None,
    reason="install.sh is the Linux installer")

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"


def run_fn(tmp_path: Path, snippet: str) -> subprocess.CompletedProcess:
    """Source the script (which defines functions and runs nothing) and run `snippet`."""
    return subprocess.run(["bash", "-c", f'source "{SCRIPT}"\n{snippet}'],
                          capture_output=True, text=True, cwd=tmp_path, timeout=60)


def test_sourcing_runs_nothing(tmp_path):
    r = run_fn(tmp_path, "true")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == ""


def test_first_install_needs_media(tmp_path):
    r = run_fn(tmp_path, 'parse_args; [[ ${#MEDIA[@]} -eq 0 ]]')
    assert r.returncode == 0, r.stderr
    r = run_fn(tmp_path, 'parse_args --media /a --media "/b c" --scratch /s --user bob --encoder qsv\n'
                         'printf "%s|" "${MEDIA[@]}"; echo; printf "%s|" "${SCRATCH[@]}"; echo\n'
                         'echo "$RUN_USER $ENCODER $UNINSTALL $REMOVE_DATA"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["/a|/b c|", "/s|", "bob qsv 0 0"]


@pytest.mark.parametrize("args, message", [
    ("--encoder amf", "nvenc or qsv"),
    ("--user root", "root"),
    ("--remove-data", "--uninstall"),
    ("--frobnicate", "Unknown option"),
])
def test_bad_options_are_refused(tmp_path, args, message):
    r = run_fn(tmp_path, f"parse_args {args}")
    assert r.returncode == 1
    assert message in r.stderr and "Setup did not finish" in r.stderr


def test_uninstall_with_remove_data_is_accepted(tmp_path):
    r = run_fn(tmp_path, 'parse_args --uninstall --remove-data; echo "$UNINSTALL $REMOVE_DATA"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1 1"


def test_user_comes_from_the_folder_owner(tmp_path):
    r = run_fn(tmp_path, f'choose_user "{tmp_path}"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == getpass.getuser()


def test_root_owned_folder_asks_for_user(tmp_path):
    r = run_fn(tmp_path, "choose_user /")
    assert r.returncode == 1 and "--user" in r.stderr


def test_owner_with_no_account_asks_for_user(tmp_path):
    # A NAS mount often hands out user IDs that have no account on this machine.
    r = run_fn(tmp_path, f'stat() {{ echo 4242; }}\nchoose_user "{tmp_path}"')
    assert r.returncode == 1 and "--user" in r.stderr


@pytest.mark.parametrize("line, new_enough", [
    ("ffmpeg version 8.0.1-3ubuntu2 Copyright (c) 2000-2025", True),
    ("ffmpeg version 7.1.2-0+deb13u1 Copyright", True),
    ("ffmpeg version 9.0.2-full_build-www.gyan.dev Copyright", True),
    ("ffmpeg version N-118000-gabc Copyright", True),   # a git build: assume it's recent
    ("ffmpeg version n7.1.1 Copyright", True),
    ("ffmpeg version 7.1 Copyright", True),
    ("ffmpeg version 6.1.1-3ubuntu5 Copyright", False),
    ("ffmpeg version 7.0.2 Copyright", False),
])
def test_ffmpeg_version_check(tmp_path, line, new_enough):
    r = run_fn(tmp_path, f'ffmpeg_new_enough "{line}"')
    assert r.returncode == (0 if new_enough else 1), r.stderr


@pytest.mark.parametrize("path, inside", [
    ("/opt/shelfsaver", True),
    ("/opt/shelfsaver/src", True),
    ("/opt/shelfsaver.old/x", True),
    ("/opt/shelfsaver.new", True),
    ("/opt/shelfsaver2", False),
    ("/home/u/ShelfSaver", False),
])
def test_refuses_to_run_from_inside_the_program_folder(tmp_path, path, inside):
    r = run_fn(tmp_path, f'inside_app_dir "{path}"')
    assert r.returncode == (0 if inside else 1), r.stderr
