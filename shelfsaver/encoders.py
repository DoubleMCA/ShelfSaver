"""The hardware H.265 encoders ShelfSaver can drive, and how ffmpeg is told to use each.

Why this module exists: the ffmpeg command used to hard-code NVIDIA's NVENC (encoder name,
rate-control flags, pixel format, the `nvidia-smi` GPU tile, the self-test). Intel Quick Sync
(QSV) and others need a different command, and need it *per operating system* (NVENC is the same
on Windows and Linux; QSV starts its device differently on each). Putting everything an encoder
decides behind one small interface means `encode.build_command` stays a single readable function
and adding an encoder touches this file, not the whole app.

What an encoder decides, and what it does not:
- It decides: how to start its hardware (`input_args`), the encoder options (`codec_args`),
  any video filters it needs (`filters`), how to test itself (`self_test_args`), and how to read
  its load and name for the status pages (`usage_percent`, `device_info`).
- It does not decide: stream mapping, containers, subtitles, audio, colour tags, or the
  quality *level* names. Those are the same for every encoder, so they stay in `encode.py`.

The NVENC command must stay byte-for-byte what it was before this module existed.
`tests/test_encoders.py` checks that against a recorded snapshot of the original commands.
"""
from __future__ import annotations

from typing import Callable, Optional

from .quality import LEVELS

# Runs a small diagnostic program and returns its text output, or None if it could not run.
# (mcp_server._run_tool). Passed in so tests can replace it and nothing here spawns a process.
RunTool = Callable[[list], Optional[str]]


class Encoder:
    """One hardware H.265 encoder. Subclasses fill in the details."""

    name = ""    # short id used in settings and logs, e.g. "nvenc"
    label = ""   # what people see, e.g. "NVIDIA NVENC"
    codec = ""   # the ffmpeg encoder name, e.g. "hevc_nvenc"

    def input_args(self) -> list:
        """Arguments that must come before the first `-i` (hardware device setup)."""
        return []

    def codec_args(self, stream: int, level: str, deep: bool) -> list:
        """`-c:v:<stream> <codec>` plus its rate control, pixel format and profile.

        `level` is "low"/"medium"/"high"; `deep` is true for 10-bit sources (HDR and similar),
        which must be kept at 10 bits or banding appears in dark gradients.
        """
        raise NotImplementedError

    def filters(self, deinterlace: bool) -> list:
        """Video filters, in order, to run before encoding (joined with commas by the caller)."""
        raise NotImplementedError

    def self_test_args(self) -> list:
        """Arguments that encode one blank frame, placed after a `-f lavfi -i color=...` input."""
        raise NotImplementedError

    def usage_percent(self, run_tool: RunTool) -> Optional[int]:
        """How busy the hardware is, 0-100, or None when it cannot be read."""
        return None

    def device_info(self, run_tool: RunTool) -> dict:
        """{"available": bool, "name": str|None, "driver": str|None} for the health page."""
        return {"available": False, "name": None, "driver": None}


class Nvenc(Encoder):
    """NVIDIA NVENC. Identical on Windows and Linux: the driver exposes it with no device setup."""

    name = "nvenc"
    label = "NVIDIA NVENC"
    codec = "hevc_nvenc"

    def codec_args(self, stream: int, level: str, deep: bool) -> list:
        args = [
            f"-c:v:{stream}", self.codec, "-preset", "p7", "-tune", "hq",
            "-rc", "vbr", "-cq", str(LEVELS[level].cq), "-b:v", "0",
            "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32",
        ]
        if deep:
            args += [f"-pix_fmt:v:{stream}", "p010le", f"-profile:v:{stream}", "main10"]
        else:
            args += [f"-pix_fmt:v:{stream}", "yuv420p", f"-profile:v:{stream}", "main"]
        return args

    def filters(self, deinterlace: bool) -> list:
        # NVENC takes ordinary system-memory frames, so the plain software deinterlacer works.
        return ["bwdif"] if deinterlace else []

    def self_test_args(self) -> list:
        return ["-frames:v", "1", "-c:v", self.codec]

    def usage_percent(self, run_tool: RunTool) -> Optional[int]:
        """The busier of the card and its video encoder: encoding barely shows as general use."""
        out = run_tool(["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder",
                        "--format=csv,noheader,nounits"])
        lines = (out or "").strip().splitlines()
        if not lines:
            return None
        numbers = [int(part) for part in lines[0].split(",") if part.strip().isdigit()]
        return max(numbers) if numbers else None

    def device_info(self, run_tool: RunTool) -> dict:
        out = run_tool(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
        line = (out or "").strip().splitlines()
        if not line:
            return {"available": False, "name": None, "driver": None}
        name, _, driver = line[0].partition(",")
        return {"available": True, "name": name.strip(), "driver": driver.strip()}


# Every encoder ShelfSaver knows, by `name`. The first is the default and what runs today.
ENCODERS = {"nvenc": Nvenc}
DEFAULT_NAME = "nvenc"


def encoder_for(config) -> Encoder:
    """The encoder the settings ask for. An unknown name falls back to the default rather than
    stopping the app: config.load_config already drops names it does not recognise."""
    return ENCODERS.get(getattr(config, "encoder", DEFAULT_NAME), ENCODERS[DEFAULT_NAME])()


DEFAULT_ENCODER = ENCODERS[DEFAULT_NAME]()
