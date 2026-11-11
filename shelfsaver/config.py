"""Application configuration."""
from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .encoders import DEFAULT_NAME, ENCODERS


@dataclass
class Config:
    data_dir: Path
    port: int = 8642
    # Which network address the web page listens on. 0.0.0.0 = every network the machine is on
    # (what ShelfSaver has always done, and what a container needs, because Docker's port
    # mapping arrives on the container's own network). 127.0.0.1 = only this machine, for someone
    # who puts a reverse proxy in front on the same machine.
    host: str = "0.0.0.0"
    # Reverse proxies allowed to tell ShelfSaver the real client address and whether the visitor
    # used HTTPS (via X-Forwarded-For / X-Forwarded-Proto). Comma-separated addresses or networks,
    # or "*". Empty = trust nobody, which is the safe default: a header from an untrusted sender
    # is just a claim, and believing it would let anyone dodge the login rate limit.
    trusted_proxies: str = ""
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    # Which hardware encoder to use: a key of encoders.ENCODERS.
    encoder: str = DEFAULT_NAME
    # QSV only. Linux: the render node (default /dev/dri/renderD128). Windows: the DirectX adapter
    # number when the PC has more than one GPU (default: Windows picks).
    qsv_device: str = ""
    # Folders the web UI may browse and libraries may live in. Empty means "no restriction"
    # on Windows (drive letters, as before) and "nothing allowed yet" on Linux; see
    # web/folders.py for why Linux must be told explicitly.
    media_roots: list = field(default_factory=list)
    # Where the scratch (working) folder may be. Kept apart from media_roots because scratch is
    # usually its own fast disk, mounted separately (in Docker, e.g. /scratch), and must not be
    # allowed to double as a library location.
    scratch_roots: list = field(default_factory=list)


def _default_data_dir() -> Path:
    env = os.environ.get("SHELFSAVER_DATA")
    if env:
        return Path(env)
    program_data = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA")
    if program_data:
        return Path(program_data) / "ShelfSaver"
    if os.name != "nt":
        # XDG: user data lives under ~/.local/share, not loose in the home folder. (Docker
        # sets SHELFSAVER_DATA=/config, so this default is for a plain Linux install.)
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        return base / "ShelfSaver"
    return Path.home() / "ShelfSaver"


def _valid_port(value) -> int | None:
    """A usable TCP port from a config value, or None. Rejects bool (True == 1 in Python)."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 and not isinstance(value, bool) else None


def _valid_host(value) -> str | None:
    """A listen address (IPv4 or IPv6 literal), or None. A hostname is refused on purpose: which
    address it resolves to can change, and a typo should fall back to the default, not stop the
    web page from starting."""
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _valid_proxies(value) -> str | None:
    """A comma-separated list of addresses/networks (or "*"), normalised, or None if any entry is
    bad. All-or-nothing: silently dropping one bad entry could trust more or fewer proxies than
    the person meant."""
    if not isinstance(value, str) or not value.strip():
        return None
    entries = [part.strip() for part in value.split(",")]
    for entry in entries:
        if entry == "*":
            continue
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            return None
    return ",".join(entries)


def _clean_roots(values) -> list:
    """Keep only non-empty strings, so a bad config.json entry cannot break startup."""
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def load_config() -> Config:
    cfg = Config(data_dir=_default_data_dir())
    path = cfg.data_dir / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        for key in ("ffmpeg", "ffprobe"):
            value = data.get(key)
            if isinstance(value, str) and value:
                setattr(cfg, key, value)
        for key in ("media_roots", "scratch_roots"):
            if isinstance(data.get(key), list):
                setattr(cfg, key, _clean_roots(data[key]))
        # Cleaned exactly like the environment variable below, so the two always agree.
        file_encoder = data.get("encoder")
        if isinstance(file_encoder, str) and file_encoder.strip().lower() in ENCODERS:
            cfg.encoder = file_encoder.strip().lower()
        device = data.get("qsv_device")
        if isinstance(device, (str, int)) and not isinstance(device, bool):
            cfg.qsv_device = str(device).strip()
        if _valid_port(data.get("port")):
            cfg.port = _valid_port(data["port"])
        if _valid_host(data.get("host")):
            cfg.host = _valid_host(data["host"])
        if _valid_proxies(data.get("trusted_proxies")):
            cfg.trusted_proxies = _valid_proxies(data["trusted_proxies"])
    # The environment wins over the file: in a container it is the normal way to configure,
    # and it lets the same image point at different media without editing a file.
    for key, variable in (("media_roots", "SHELFSAVER_MEDIA_ROOTS"),
                          ("scratch_roots", "SHELFSAVER_SCRATCH_ROOTS")):
        value = os.environ.get(variable)
        if value:
            setattr(cfg, key, _clean_roots(value.split(os.pathsep)))
    # An unknown name is ignored, not fatal: the file's value (or the default) stays in force.
    if os.environ.get("SHELFSAVER_ENCODER", "").strip().lower() in ENCODERS:
        cfg.encoder = os.environ["SHELFSAVER_ENCODER"].strip().lower()
    if os.environ.get("SHELFSAVER_QSV_DEVICE", "").strip():
        cfg.qsv_device = os.environ["SHELFSAVER_QSV_DEVICE"].strip()
    if _valid_port(os.environ.get("SHELFSAVER_PORT")):
        cfg.port = _valid_port(os.environ["SHELFSAVER_PORT"])
    if _valid_host(os.environ.get("SHELFSAVER_HOST")):
        cfg.host = _valid_host(os.environ["SHELFSAVER_HOST"])
    if _valid_proxies(os.environ.get("SHELFSAVER_TRUSTED_PROXIES")):
        cfg.trusted_proxies = _valid_proxies(os.environ["SHELFSAVER_TRUSTED_PROXIES"])
    return cfg
