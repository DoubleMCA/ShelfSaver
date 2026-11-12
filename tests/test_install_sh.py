"""The Linux installer's helpers. Only pieces of the script run here: as a whole it needs root
and changes the machine, so the full install is checked end-to-end by hand (see the README)."""
from __future__ import annotations

import getpass
import json
import os
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


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="as root the folder is root's, which is refused (tested below)")
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


def _write_config(tmp_path, existing, media=(), scratch=()):
    cfg = tmp_path / "config.json"
    if existing is not None:
        cfg.write_text(existing, encoding="utf-8")
    arrays = "MEDIA=(" + " ".join(f"'{m}'" for m in media) + ")\n"
    arrays += "SCRATCH=(" + " ".join(f"'{s}'" for s in scratch) + ")\n"
    r = run_fn(tmp_path, arrays + f'write_config "{cfg}" qsv /usr/bin/ffmpeg /usr/bin/ffprobe')
    assert r.returncode == 0, r.stderr
    return cfg, r


def test_config_keeps_other_keys_and_sets_ours(tmp_path):
    cfg, _ = _write_config(tmp_path, '{"port": 9000, "encoder": "nvenc", "media_roots": ["/old"]}',
                           media=["/srv/my videos/é"])
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["port"] == 9000 and data["encoder"] == "qsv"
    assert data["media_roots"] == ["/srv/my videos/é"]
    assert data["ffmpeg"] == "/usr/bin/ffmpeg" and data["ffprobe"] == "/usr/bin/ffprobe"
    assert "scratch_roots" not in data
    assert cfg.read_text(encoding="utf-8").endswith("\n")


def test_update_without_media_keeps_the_existing_roots(tmp_path):
    cfg, _ = _write_config(tmp_path, '{"media_roots": ["/old"], "scratch_roots": ["/s"]}')
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["media_roots"] == ["/old"] and data["scratch_roots"] == ["/s"]


@pytest.mark.parametrize("content", ["{nope", "[1,2]"])
def test_unreadable_config_is_kept_as_bad(tmp_path, content):
    cfg, r = _write_config(tmp_path, content, media=["/m"])
    assert (tmp_path / "config.json.bad").read_text(encoding="utf-8") == content
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["media_roots"] == ["/m"] and data["encoder"] == "qsv"
    assert "NOTE:" in r.stdout


@pytest.mark.parametrize("content, has", [
    ('{"media_roots": ["/a"]}', True),
    ('{"media_roots": []}', False),
    ('{"port": 1}', False),
    (None, False),
])
def test_config_has_media(tmp_path, content, has):
    cfg = tmp_path / "config.json"
    if content is not None:
        cfg.write_text(content, encoding="utf-8")
    r = run_fn(tmp_path, f'config_has_media "{cfg}"')
    assert r.returncode == (0 if has else 1), r.stderr


def test_render_groups(tmp_path):
    for name in ("renderD128", "renderD129"):
        (tmp_path / name).touch()
    gid = (tmp_path / "renderD128").stat().st_gid
    r = run_fn(tmp_path, f'DRI_GLOB="{tmp_path}/renderD*" render_groups')
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ([] if gid == 0 else [str(gid)])
    r = run_fn(tmp_path, f'DRI_GLOB="{tmp_path}/nothing*" render_groups')
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_service_file(tmp_path):
    r = run_fn(tmp_path, 'service_unit jason jason "44 993"')
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    for expected in ("User=jason", "Group=jason", "SupplementaryGroups=44 993",
                     "ExecStart=/opt/shelfsaver/venv/bin/python -m shelfsaver",
                     "WorkingDirectory=/opt/shelfsaver",
                     "Environment=SHELFSAVER_DATA=/var/lib/shelfsaver", "Restart=always",
                     "RestartSec=10", "TimeoutStopSec=60", "NoNewPrivileges=yes",
                     "ProtectSystem=full", "PrivateTmp=yes", "WantedBy=multi-user.target"):
        assert expected in lines, expected
    assert "ProtectHome" not in r.stdout
    r = run_fn(tmp_path, 'service_unit jason users ""')
    assert "Group=users" in r.stdout.splitlines() and "SupplementaryGroups" not in r.stdout


def test_rollback_restarts_a_service_stopped_before_the_folders_were_swapped(tmp_path):
    # An update can fail after the running version was stopped but before anything moved
    # (another program holding the port). The old version must not stay stopped.
    unit = tmp_path / "shelfsaver.service"
    unit.write_text("[Service]\n", encoding="utf-8")
    log = tmp_path / "calls.log"
    r = run_fn(tmp_path, f'systemctl() {{ echo "systemctl $*" >> "{log}"; }}\n'
                         f'UNIT_PATH="{unit}"; STOPPED_OLD=1; OLD_MOVED=0; NEW_IN_PLACE=0\n'
                         'rollback')
    assert r.returncode == 0, r.stderr
    assert "systemctl start shelfsaver" in log.read_text(encoding="utf-8").splitlines()


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
