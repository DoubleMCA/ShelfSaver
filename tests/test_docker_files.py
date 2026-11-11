"""The container files: the start-up script and the Unraid template.

The image itself is built and run by hand (it needs Docker); these tests keep the parts that
can silently rot, because nothing else in the suite imports them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker-entrypoint.sh"
TEMPLATE = ROOT / "docker" / "shelfsaver-unraid.xml"

needs_sh = pytest.mark.skipif(
    os.name == "nt" or not shutil.which("sh"), reason="the entrypoint is a Linux shell script")


def run_entrypoint(*command, **env):
    clean = {"PATH": os.environ["PATH"], **env}
    return subprocess.run(["sh", str(ENTRYPOINT), *command], env=clean, capture_output=True,
                          text=True, timeout=20)


@needs_sh
@pytest.mark.parametrize("name", ["PUID", "PGID", "UMASK"])
def test_entrypoint_refuses_non_numeric_ids(name):
    """These values reach chown/setpriv arguments, so anything but digits must stop start-up."""
    result = run_entrypoint("echo", "started", **{name: "1;touch x"})
    assert result.returncode == 64
    assert "must be numbers" in result.stderr
    assert "started" not in result.stdout


@needs_sh
@pytest.mark.skipif(os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="as root the script would try to chown and drop privileges")
def test_entrypoint_runs_the_command_as_is_when_not_root(tmp_path):
    """With Docker's `user:` option there is nothing to drop; it must just hand over."""
    result = run_entrypoint("sh", "-c", 'echo "home=$HOME umask=$(umask)"',
                            SHELFSAVER_DATA=str(tmp_path), UMASK="027")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"home={tmp_path} umask=0027"


def _template():
    return ET.parse(TEMPLATE).getroot()


def test_unraid_template_is_valid_xml_with_the_essentials():
    root = _template()
    assert root.tag == "Container"
    assert root.findtext("Name") == "ShelfSaver"
    assert root.findtext("Repository")
    targets = {c.get("Target"): c.get("Type") for c in root.findall("Config")}
    # The three places data lives, and the web page.
    assert targets["/config"] == "Path" and targets["/media"] == "Path"
    assert targets["8642"] == "Port"


def test_unraid_template_only_sets_variables_the_app_reads():
    """A typo in a variable name would be silently ignored by the app, so pin the names."""
    known = {"SHELFSAVER_ENCODER", "SHELFSAVER_QSV_DEVICE", "SHELFSAVER_PORT", "PUID", "PGID",
             "UMASK", "NVIDIA_VISIBLE_DEVICES"}
    used = {c.get("Target") for c in _template().findall("Config") if c.get("Type") == "Variable"}
    assert used <= known, used - known


def test_template_default_port_matches_the_app_default():
    from shelfsaver.config import Config
    port = [c for c in _template().findall("Config") if c.get("Type") == "Port"][0]
    assert port.get("Target") == str(Config(data_dir=Path(".")).port)


# ---- the image build/publish workflow (text checks: no YAML library is a dependency)

WORKFLOW = ROOT / ".github" / "workflows" / "docker-image.yml"


def _workflow_text():
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_actions_are_pinned_to_exact_commits():
    """A tag like @v4 can be moved to different code later; a commit hash cannot."""
    import re

    uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", _workflow_text(), flags=re.MULTILINE)
    assert uses, "expected the workflow to use actions"
    for reference in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", reference), reference


def test_workflow_uses_only_the_built_in_token_and_never_runs_on_pull_request_target():
    """pull_request_target runs with write access on code from forks; the plain token is the only
    secret allowed, so a leak of anything else is impossible."""
    import re

    text = _workflow_text()
    assert "pull_request_target" not in text
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"GITHUB_TOKEN"}


def test_workflow_only_publishes_for_releases_and_never_for_pull_requests():
    text = _workflow_text()
    steps = text.split(chr(10) + "      - ")[1:]          # one entry per step in the job
    publishing = [step for step in steps if "name: Log in to the registry" in step
                  or "name: Publish" in step]
    assert len(publishing) == 2
    # Both must be gated on the computed flag, which the script sets to true only for a v* tag
    # push, the weekly run or a manual run, and only in the real repository.
    for step in publishing:
        assert "if: steps.release.outputs.publish == 'true'" in step
    assert text.count("push: true") == 1 and "push: false" in text
    assert "linux/amd64" in text


def test_image_name_in_workflow_matches_the_unraid_template():
    import re

    image = re.search(r"IMAGE:\s*(\S+)", _workflow_text()).group(1)
    assert _template().findtext("Repository").startswith(image + ":")


def test_dockerfile_health_check_asks_healthz():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "/healthz" in text.split("HEALTHCHECK", 1)[1]
