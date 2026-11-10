"""Which folders the web UI may browse and libraries may use (roots mode).

Roots mode is how Linux always works and how Windows works when media roots are configured, so
these tests run on both. They check the security property that matters: nothing outside a
configured root can be browsed or used, including by way of a symlink.
"""
from __future__ import annotations

import os

import pytest

from shelfsaver import hostos
from shelfsaver.config import load_config
from shelfsaver.web import folders
from shelfsaver.web.folders import FolderProblem


@pytest.fixture
def tree(tmp_path):
    """media/Movies, media/.hidden, and a sibling 'secret' folder outside the media root."""
    media = tmp_path / "media"
    (media / "Movies").mkdir(parents=True)
    (media / ".hidden").mkdir()
    (tmp_path / "secret").mkdir()
    return tmp_path, media


def refused(call, *args):
    with pytest.raises(FolderProblem) as caught:
        call(*args)
    return caught.value


# ---- check_local_folder

def test_a_folder_inside_a_root_is_accepted(tree):
    _, media = tree
    assert folders.check_local_folder(str(media / "Movies"), "library", [str(media)]) == str(media / "Movies")


def test_a_folder_outside_every_root_is_refused_and_the_roots_are_named(tree):
    tmp, media = tree
    problem = refused(folders.check_local_folder, str(tmp / "secret"), "library", [str(media)])
    assert problem.status == 400 and str(media) in problem.message


def test_a_sibling_whose_name_starts_like_a_root_is_not_inside_it(tree):
    # "/x/media2" must not pass just because "/x/media" is a root.
    tmp, media = tree
    (tmp / "media2").mkdir()
    assert refused(folders.check_local_folder, str(tmp / "media2"), "library", [str(media)])


def test_dot_dot_cannot_climb_out_of_a_root(tree):
    tmp, media = tree
    sneaky = str(media / "Movies" / ".." / ".." / "secret")
    assert refused(folders.check_local_folder, sneaky, "library", [str(media)])


def test_a_symlink_inside_a_root_cannot_lead_outside_it(tree):
    tmp, media = tree
    try:
        (media / "escape").symlink_to(tmp / "secret", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode on this machine")
    assert refused(folders.check_local_folder, str(media / "escape"), "library", [str(media)])


def test_a_relative_path_is_refused(tree):
    _, media = tree
    assert refused(folders.check_local_folder, "Movies", "library", [str(media)]).status == 400


def test_a_missing_folder_is_refused(tree):
    _, media = tree
    assert "can't be found" in refused(folders.check_local_folder, str(media / "nope"), "library",
                                       [str(media)]).message


def test_an_empty_path_is_refused(tree):
    _, media = tree
    assert refused(folders.check_local_folder, "   ", "library", [str(media)]).status == 400


@pytest.mark.skipif(hostos.IS_WINDOWS, reason="Windows without roots keeps drive-letter mode")
def test_linux_with_no_roots_refuses_everything_and_says_what_to_set(tree):
    tmp, _ = tree
    problem = refused(folders.check_local_folder, str(tmp), "library", [])
    assert "SHELFSAVER_MEDIA_ROOTS" in problem.message
    assert "SHELFSAVER_SCRATCH_ROOTS" in refused(
        folders.check_local_folder, str(tmp), "scratch", [], "SHELFSAVER_SCRATCH_ROOTS").message


# ---- browse

def test_browsing_with_no_path_lists_the_roots(tree):
    _, media = tree
    body = folders.browse(None, [str(media)])
    assert body == {"path": None, "parent": None,
                    "folders": [{"name": str(media), "path": str(media)}]}


def test_a_root_that_does_not_exist_is_not_offered(tree):
    tmp, media = tree
    shown = folders.browse(None, [str(media), str(tmp / "gone")])["folders"]
    assert [f["path"] for f in shown] == [str(media)]


def test_browsing_a_root_lists_folders_sorted_and_hides_dot_folders(tree):
    _, media = tree
    (media / "tv").mkdir()
    (media / "file.mkv").write_bytes(b"x")
    body = folders.browse(str(media), [str(media)])
    names = [f["name"] for f in body["folders"]]
    assert names == ["Movies", "tv"] or hostos.IS_WINDOWS  # Windows shows dot-folders
    assert "file.mkv" not in names
    assert body["parent"] is None  # "up" from a root returns to the root list


def test_a_subfolder_has_its_real_parent(tree):
    _, media = tree
    assert folders.browse(str(media / "Movies"), [str(media)])["parent"] == str(media)


def test_browsing_outside_the_roots_is_refused(tree):
    tmp, media = tree
    assert refused(folders.browse, str(tmp / "secret"), [str(media)]).status == 400
    assert refused(folders.browse, str(tmp), [str(media)]).status == 400


def test_browsing_cannot_climb_out_with_dot_dot(tree):
    tmp, media = tree
    assert refused(folders.browse, str(media / ".." / "secret"), [str(media)])


def test_a_symlinked_folder_is_not_listed_so_the_picker_cannot_be_walked_out(tree):
    tmp, media = tree
    try:
        (media / "escape").symlink_to(tmp / "secret", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode on this machine")
    assert "escape" not in [f["name"] for f in folders.browse(str(media), [str(media)])["folders"]]


def test_browsing_a_missing_folder_is_a_plain_400(tree):
    _, media = tree
    assert refused(folders.browse, str(media / "nope"), [str(media)]).status == 400


# ---- configuration

def test_roots_come_from_the_environment_and_beat_the_file(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text('{"media_roots": ["/from/file"], "scratch_roots": ["/s"]}')
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    assert load_config().media_roots == ["/from/file"]
    monkeypatch.setenv("SHELFSAVER_MEDIA_ROOTS", os.pathsep.join(["/media", "/more"]))
    cfg = load_config()
    assert cfg.media_roots == ["/media", "/more"] and cfg.scratch_roots == ["/s"]


def test_bad_root_entries_in_the_file_are_ignored_not_fatal(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text('{"media_roots": ["/ok", 5, "", null, "  "]}')
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    monkeypatch.delenv("SHELFSAVER_MEDIA_ROOTS", raising=False)
    assert load_config().media_roots == ["/ok"]
