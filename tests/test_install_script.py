"""The installer's FFmpeg check. Only pieces of the script run here: as a whole it needs
administrator rights and changes the PC."""
from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install.ps1"

HARNESS = r"""
param([string]$Script, [string]$Candidates)
$ErrorActionPreference = 'Stop'
$tokens = $null; $problems = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$tokens, [ref]$problems)
if ($problems.Count -gt 0) { Write-Output "PARSE ERRORS: $($problems.Count)"; exit 3 }
foreach ($name in 'Test-HasGpuEncoder', 'Find-EncodingFFmpeg') {
    $found = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    Invoke-Expression $found.Extent.Text
}
function Find-ToolCandidates($name, $config) { return ($Candidates -split '\|') }
$chosen = Find-EncodingFFmpeg $null
Write-Output "CHOSEN:$chosen"
"""


def fake_ffmpeg(folder: Path, name: str, encoders: str, code: int = 0) -> Path:
    path = folder / f"{name}.cmd"
    path.write_text(f"@echo off\r\necho {encoders}\r\necho noise 1>&2\r\nexit /b {code}\r\n",
                    encoding="ascii")
    return path


def choose(tmp_path: Path, *candidates: Path) -> str:
    harness = tmp_path / "harness.ps1"
    harness.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(harness), "-Script", str(SCRIPT),
         "-Candidates", "|".join(str(c) for c in candidates)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(l for l in result.stdout.splitlines() if l.startswith("CHOSEN:"))
    return line[len("CHOSEN:"):]


def test_ffmpeg_without_the_gpu_encoder_is_passed_over_for_one_that_has_it(tmp_path):
    without = fake_ffmpeg(tmp_path, "partial", "V..... libx265  x265")
    good = fake_ffmpeg(tmp_path, "full", "V..... hevc_nvenc  NVIDIA NVENC")
    assert choose(tmp_path, without, good) == str(good)


def test_no_usable_ffmpeg_is_not_chosen(tmp_path):
    assert choose(tmp_path, fake_ffmpeg(tmp_path, "partial", "V..... libx265  x265")) == ""


def test_ffmpeg_that_reports_failure_is_not_trusted(tmp_path):
    broken = fake_ffmpeg(tmp_path, "broken", "V..... hevc_nvenc  NVIDIA NVENC", code=1)
    assert choose(tmp_path, broken) == ""


def test_setup_stops_with_a_plain_message_when_no_ffmpeg_can_encode():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "can't convert video on the NVIDIA graphics card" in text


def test_setup_opens_the_port_on_every_network_type():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "-LocalPort $Port -Profile Domain,Private,Public" in text
