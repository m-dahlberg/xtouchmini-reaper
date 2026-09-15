"""config.toml for the daemon.

Strict about unknown keys, the way the ShuttleXpress bridge is: a typo in a
config file should say so rather than silently doing nothing.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .engine import Feel
from .protocol import Encoding

ENV_OVERRIDE = "XTOUCHMINI_CONFIG"


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "xtouchmini"


def default_path() -> Path:
    override = os.environ.get(ENV_OVERRIDE)
    return Path(override) if override else config_dir() / "config.toml"


@dataclass(frozen=True)
class OscConfig:
    host: str = "127.0.0.1"
    # REAPER listens here; must match the control surface's receive port.
    send_port: int = 8010
    # We listen here for REAPER's feedback.
    listen_host: str = "127.0.0.1"
    listen_port: int = 9010


@dataclass(frozen=True)
class DeviceConfig:
    # Matched against the rtmidi port name, case-insensitively. Never match on
    # the ALSA card id: this machine has a RODE "Mini" alongside the X-Touch
    # "MINI", and the two differ only in case.
    port_match: str = "X-TOUCH MINI"
    # Set in the X-TOUCH Editor. See docs/protocol.md -- the encoders ship
    # absolute, which has dead zones at both ends of their travel.
    encoding: str = Encoding.RELATIVE_1.value
    # Reconnect polling while the device is unplugged.
    reconnect_interval: float = 1.0


@dataclass(frozen=True)
class Config:
    osc: OscConfig = field(default_factory=OscConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    feel: Feel = field(default_factory=Feel)
    state_path: Path = field(default_factory=lambda: config_dir() / "state.json")

    @property
    def encoding(self) -> Encoding:
        return Encoding(self.device.encoding)


def _section(cls, raw: dict, name: str):
    """Build a dataclass from a table, rejecting keys it does not define."""
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            f"[{name}] has unknown key(s): {', '.join(sorted(unknown))}")
    return cls(**raw)


def load(path: Path | None = None) -> Config:
    path = Path(path) if path else default_path()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return Config()
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: {exc}") from exc

    unknown = set(raw) - {"osc", "device", "feel", "state_path"}
    if unknown:
        raise ValueError(f"unknown section(s): {', '.join(sorted(unknown))}")

    cfg = Config(
        osc=_section(OscConfig, raw.get("osc", {}), "osc"),
        device=_section(DeviceConfig, raw.get("device", {}), "device"),
        feel=_section(Feel, raw.get("feel", {}), "feel"),
        state_path=Path(raw.get("state_path") or config_dir() / "state.json"),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    try:
        Encoding(cfg.device.encoding)
    except ValueError:
        allowed = ", ".join(e.value for e in Encoding)
        raise ValueError(
            f"[device] encoding must be one of: {allowed}") from None
    if cfg.osc.send_port == cfg.osc.listen_port:
        # They would form a loop: our own writes would come straight back as
        # feedback and fight the controller.
        raise ValueError("[osc] send_port and listen_port must differ")
    for name, port in (("send_port", cfg.osc.send_port),
                       ("listen_port", cfg.osc.listen_port)):
        if not 1 <= port <= 65535:
            raise ValueError(f"[osc] {name} is out of range: {port}")
    if cfg.feel.coarse_step <= 0 or cfg.feel.fine_step <= 0:
        raise ValueError("[feel] step sizes must be positive")
    if cfg.feel.fine_step > cfg.feel.coarse_step:
        raise ValueError("[feel] fine_step must be smaller than coarse_step")
    if not 0 <= cfg.feel.pickup_epsilon <= 1:
        raise ValueError("[feel] pickup_epsilon must be between 0 and 1")
