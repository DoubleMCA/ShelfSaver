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

import os
import re
from typing import Callable, Optional

from . import hostos
from .quality import LEVELS

# Runs a small diagnostic program and returns its text output, or None if it could not run.
# (mcp_server._run_tool). Passed in so tests can replace it and nothing here spawns a process.
RunTool = Callable[[list], Optional[str]]

# Where Linux describes each graphics device; a render node's vendor id is in <node>/device/vendor.
SYSFS_DRM = "/sys/class/drm"
AMD_VENDOR_ID = "0x1002"


class Encoder:
    """One hardware H.265 encoder. Subclasses fill in the details."""

    name = ""    # short id used in settings and logs, e.g. "nvenc"
    label = ""   # what people see, e.g. "NVIDIA NVENC"
    codec = ""   # the ffmpeg encoder name, e.g. "hevc_nvenc"

    @classmethod
    def from_config(cls, config) -> "Encoder":
        """Build the encoder from the app settings. Most need none; QSV reads its device."""
        return cls()

    @classmethod
    def supported(cls) -> bool:
        """Whether this encoder can exist on this operating system at all. Automatic choice skips
        an unsupported one without starting FFmpeg."""
        return True

    def auto_ok(self) -> bool:
        """Whether automatic choice may pick this encoder on this machine (a pinned one is always
        used). Only VAAPI says no: on a non-AMD card it would be another vendor's driver."""
        return True

    def input_args(self) -> list:
        """Arguments that must come before the first `-i` (hardware device setup)."""
        return []

    def codec_args(self, stream: int, level: str, deep: bool) -> list:
        """`-c:v:<stream> <codec>` plus its rate control, pixel format and profile.

        `level` is "low"/"medium"/"high"; `deep` is true for 10-bit sources (HDR and similar),
        which must be kept at 10 bits or banding appears in dark gradients.
        """
        raise NotImplementedError

    def filters(self, deinterlace: bool, deep: bool = False) -> list:
        """Video filters, in order, to run before encoding (joined with commas by the caller).

        `deep` is true for 10-bit sources; only encoders that upload frames to the GPU themselves
        (VAAPI) need it, to pick the upload format.
        """
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

    def filters(self, deinterlace: bool, deep: bool = False) -> list:
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


class Qsv(Encoder):
    """Intel Quick Sync Video (iGPUs and Arc cards), on Windows and on Linux.

    Checked two ways: against FFmpeg's own documentation (the "QSV Encoders" section and the
    `-init_hw_device` section of ffmpeg.texi) and by encoding on real hardware (an Intel UHD 770
    under Linux, FFmpeg 7.1 with Intel's iHD driver; an Intel Arc Pro B70 under Windows 11,
    FFmpeg 9.0.2, in a PC whose first D3D11 adapter is an AMD card, which the default spec skipped
    on its own).

    How it differs from NVENC:
    - Device: NVENC needs none. QSV is started on a named device, which is operating-system
      specific. Linux: a DRM render node such as /dev/dri/renderD128. Windows: a DirectX adapter,
      through D3D11 (`child_device_type=d3d11va`).
    - Frames: hevc_qsv accepts ordinary system-memory frames in nv12 or p010le (it does *not* list
      yuv420p, which NVENC uses). FFmpeg converts on the way in, so the CPU deinterlacer used for
      NVENC works unchanged and no explicit hardware upload step is needed.
    - Quality: the constant-quality mode is `-global_quality` (1 to 51, 1 is best), not `-cq`.
    """

    name = "qsv"
    label = "Intel Quick Sync (QSV)"
    codec = "hevc_qsv"

    # Quality number for each level (see quality.LEVELS). A lower number is a better picture and a
    # bigger file. These were MEASURED, not guessed: 12 thirty-second clips from 6 different H.264
    # TV episodes (720p and 1080p, 2.7-6.2 Mbps streaming sources) were encoded at each value and
    # scored with VMAF against the untouched clip (100 = identical, about 90 = very good). Averages:
    #     QSV 17 = 65% of the source size, VMAF 92.6   (looks like NVENC cq 26)
    #     QSV 19 = 47% of the source size, VMAF 91.7
    #     QSV 21 = 36% of the source size, VMAF 90.8   (looks like NVENC cq 30)
    # Quick Sync's scale is NOT NVENC's: the same number gives a very different result (QSV 29 fell
    # to VMAF 82.5, visibly softer, so nothing above 21 is offered). For the same VMAF, QSV needed
    # about 5% more bytes than NVENC at the high-quality end and about 15% more at the aggressive end.
    # Results depend heavily on the picture: at QSV 21 the size ranged from 23% to 65% of the source
    # across the six shows, and a grainy source can come out BIGGER than the original at QSV 17,
    # in which case the "must be smaller" check skips it. To tune for your own files, use
    # scripts/make_compare_clips.py --encoder qsv.
    QUALITY = {"low": 17, "medium": 19, "high": 21}
    DEFAULT_LINUX_DEVICE = "/dev/dri/renderD128"  # the first (usually only) render node

    def __init__(self, device: str = "") -> None:
        # Linux: path of a render node. Windows: DirectX adapter number, or "" for the default.
        self.device = (device or "").strip()

    @classmethod
    def from_config(cls, config) -> "Qsv":
        return cls(getattr(config, "qsv_device", ""))

    def _linux_device(self) -> str:
        return self.device or self.DEFAULT_LINUX_DEVICE

    def input_args(self) -> list:
        # `qsv:hw,...` is the documented form: MFX_IMPL_HARDWARE on the given subdevice.
        if hostos.IS_WINDOWS:
            spec = "qsv:hw,child_device_type=d3d11va"
            if self.device.isdigit():  # more than one GPU: pick the adapter by number
                spec += f",child_device={self.device}"
            return ["-init_hw_device", spec]
        return ["-init_hw_device", f"qsv:hw,child_device={self._linux_device()}"]

    def codec_args(self, stream: int, level: str, deep: bool) -> list:
        # nv12 / p010le are what hevc_qsv takes; profile main10 keeps 10-bit sources at 10 bits.
        pix, profile = ("p010le", "main10") if deep else ("nv12", "main")
        return [f"-c:v:{stream}", self.codec, "-preset", "slow",
                "-global_quality", str(self.QUALITY[level]),
                f"-pix_fmt:v:{stream}", pix, f"-profile:v:{stream}", profile]

    def filters(self, deinterlace: bool, deep: bool = False) -> list:
        # Same CPU deinterlacer as NVENC. (vpp_qsv could do this on the GPU, but it needs an
        # upload step and gains little for an unattended batch tool; kept simple on purpose.)
        return ["bwdif"] if deinterlace else []

    def self_test_args(self) -> list:
        # The blank test frame is RGB, so name the pixel format QSV takes.
        return ["-frames:v", "1", "-c:v", self.codec, "-pix_fmt", "nv12"]

    def usage_percent(self, run_tool: RunTool) -> Optional[int]:
        # Intel exposes no cheap, unprivileged "encoder busy" figure (intel_gpu_top needs extra
        # permissions). The tile shows a dash instead of a number that would mislead.
        return None

    def device_info(self, run_tool: RunTool) -> dict:
        if hostos.IS_WINDOWS:
            return {"available": None, "name": None, "driver": None}  # not probed on Windows
        device = self._linux_device()
        if not os.access(device, os.R_OK | os.W_OK):
            return {"available": False, "name": None, "driver": None}
        # vainfo, if installed, names the driver ("Intel iHD driver ... - 25.2.3").
        out = run_tool(["vainfo", "--display", "drm", "--device", device]) or ""
        match = re.search(r"Driver version:\s*(.+)", out)
        return {"available": True, "name": "Intel Quick Sync (render node)",
                "driver": match.group(1).strip() if match else None}


class Amf(Encoder):
    """AMD AMF (Radeon cards), on Windows only. On Linux, AMD cards are driven through VAAPI.

    How it differs from NVENC:
    - Device: none to set up. AMF uses its default adapter, so a PC with Ryzen built-in graphics
      *and* a Radeon card may encode on the built-in one; choosing the adapter is not offered yet.
    - Frames: hevc_amf takes ordinary system-memory frames in nv12 or p010le, so the CPU
      deinterlacer used for NVENC works unchanged.
    - Quality: constant quality is `-rc cqp` with a QP for I and P frames (0 to 51, lower is
      better), plus the slowest, best `-quality` preset.

    Tuned on an RX 9000 series (RDNA 4) card. Older Radeon cards are allowed, but their encoders
    are older and may look a little softer at the same level.
    """

    name = "amf"
    label = "AMD AMF"
    codec = "hevc_amf"

    # PLACEHOLDERS until they are measured on an RX 9070, the same way the QSV numbers were (VMAF
    # against NVENC's own levels on the same clips). Replace these and this comment then.
    QUALITY = {"low": 22, "medium": 26, "high": 30}

    @classmethod
    def supported(cls) -> bool:
        return hostos.IS_WINDOWS

    def codec_args(self, stream: int, level: str, deep: bool) -> list:
        pix, profile = ("p010le", "main10") if deep else ("nv12", "main")
        qp = str(self.QUALITY[level])
        return [f"-c:v:{stream}", self.codec, "-usage", "transcoding", "-quality", "quality",
                "-rc", "cqp", "-qp_i", qp, "-qp_p", qp,
                f"-pix_fmt:v:{stream}", pix, f"-profile:v:{stream}", profile]

    def filters(self, deinterlace: bool, deep: bool = False) -> list:
        return ["bwdif"] if deinterlace else []

    def self_test_args(self) -> list:
        # The blank test frame is RGB, so name the pixel format AMF takes.
        return ["-frames:v", "1", "-c:v", self.codec, "-pix_fmt", "nv12"]

    def usage_percent(self, run_tool: RunTool) -> Optional[int]:
        # No cheap vendor tool reports AMD's encoder load; the tile shows a dash, as for Intel.
        return None

    def device_info(self, run_tool: RunTool) -> dict:
        return {"available": None, "name": None, "driver": None}  # not probed on Windows


class Vaapi(Encoder):
    """AMD Radeon cards on Linux (and in Docker), through VAAPI and Mesa's open-source driver.

    How it differs from NVENC and QSV:
    - Device: a DRM render node, like QSV on Linux (default /dev/dri/renderD128).
    - Frames: hevc_vaapi only takes frames already on the GPU. So the filters end by converting
      to the upload format (nv12, or p010 for 10-bit) and `hwupload`; the CPU deinterlacer runs
      first, before the upload. This is why `filters` is told the bit depth.
    - Quality: constant quality is `-rc_mode CQP -qp` (lower is better).

    Intel's Linux driver answers VAAPI too, but automatic choice tries Quick Sync before this, so
    an Intel machine keeps QSV and its measured levels; this encoder is meant for AMD.
    """

    name = "vaapi"
    label = "AMD (VAAPI)"
    codec = "hevc_vaapi"

    # PLACEHOLDERS until they are measured on an RX 9070 under Linux (Mesa is a different driver
    # from AMD's Windows one, so the Windows AMF numbers don't carry over). Replace these and this
    # comment then.
    QUALITY = {"low": 22, "medium": 26, "high": 30}
    DEFAULT_DEVICE = "/dev/dri/renderD128"

    def __init__(self, device: str = "") -> None:
        self.device = (device or "").strip() or self.DEFAULT_DEVICE

    @classmethod
    def from_config(cls, config) -> "Vaapi":
        return cls(getattr(config, "vaapi_device", ""))

    @classmethod
    def supported(cls) -> bool:
        return not hostos.IS_WINDOWS

    def auto_ok(self) -> bool:
        """Only on an AMD card. Intel's driver answers VAAPI too, so an Intel machine whose Quick
        Sync is broken would otherwise be converted here, labelled AMD and with AMD's numbers.
        If the vendor can't be read (no sysfs, unusual node), allow it."""
        vendor = os.path.join(SYSFS_DRM, os.path.basename(self.device), "device", "vendor")
        try:
            with open(vendor, encoding="ascii") as f:
                return f.read().strip().lower() == AMD_VENDOR_ID
        except (OSError, ValueError):
            return True

    def input_args(self) -> list:
        return ["-init_hw_device", f"vaapi=va:{self.device}", "-filter_hw_device", "va"]

    def codec_args(self, stream: int, level: str, deep: bool) -> list:
        # No -pix_fmt: the frames arrive as GPU surfaces from hwupload (see filters).
        return [f"-c:v:{stream}", self.codec, "-rc_mode", "CQP",
                "-qp", str(self.QUALITY[level]),
                f"-profile:v:{stream}", "main10" if deep else "main"]

    def filters(self, deinterlace: bool, deep: bool = False) -> list:
        upload = ["format=p010" if deep else "format=nv12", "hwupload"]
        return (["bwdif"] if deinterlace else []) + upload

    def self_test_args(self) -> list:
        return ["-frames:v", "1", "-vf", "format=nv12,hwupload", "-c:v", self.codec]

    def usage_percent(self, run_tool: RunTool) -> Optional[int]:
        # Reading AMD's encoder load needs extra permissions (as for Intel), so the tile shows
        # a dash rather than a number that would mislead.
        return None

    def device_info(self, run_tool: RunTool) -> dict:
        if not os.access(self.device, os.R_OK | os.W_OK):
            return {"available": False, "name": None, "driver": None}
        # vainfo, if installed, names the driver ("Mesa Gallium driver ... for AMD Radeon ...").
        out = run_tool(["vainfo", "--display", "drm", "--device", self.device]) or ""
        match = re.search(r"Driver version:\s*(.+)", out)
        return {"available": True, "name": "AMD Radeon (render node)",
                "driver": match.group(1).strip() if match else None}


# Every encoder ShelfSaver knows, by `name`. DEFAULT_NAME is the fallback for helpers and tests
# that need one concrete encoder; the app itself defaults to AUTO (see encoder_choice.py).
ENCODERS = {"nvenc": Nvenc, "qsv": Qsv, "amf": Amf, "vaapi": Vaapi}
DEFAULT_NAME = "nvenc"
# The encoder setting that means "use whichever works": see encoder_choice.EncoderChoice.
AUTO = "auto"


def encoder_for(config) -> Encoder:
    """The encoder the settings ask for. An unknown name falls back to the default rather than
    stopping the app: config.load_config already drops names it does not recognise.

    AUTO also returns the fallback here. Code that should follow the automatic choice (the
    worker, the status tiles) uses encoder_choice.EncoderChoice instead."""
    cls = ENCODERS.get(getattr(config, "encoder", DEFAULT_NAME), ENCODERS[DEFAULT_NAME])
    return cls.from_config(config)


DEFAULT_ENCODER = ENCODERS[DEFAULT_NAME]()
