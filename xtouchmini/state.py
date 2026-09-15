"""Read state.json, the panel's view of what the controller should be doing.

The panel writes this file on change only, atomically. The daemon polls its
mtime once per loop iteration -- one stat() -- rather than watching with
inotify, because the worst case is a focus change taking ~50ms to reach the
daemon and no human clicks a plugin and turns an encoder faster than that.

Nothing here talks to REAPER or knows what a plugin is. See docs/ipc.md.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

LAYERS = ("A", "B")
ENCODERS = 8
BUTTONS = 8
RESERVED = 8


@dataclass(frozen=True)
class Slot:
    """One mapped encoder or button."""
    param: int
    name: str = ""
    value: float = 0.0
    default: float = 0.0
    mode: str = "toggle"          # buttons only: toggle | momentary
    taper: str = "linear"         # linear | log | exp
    sensitivity: float = 1.0

    @classmethod
    def parse(cls, raw) -> "Slot | None":
        if not isinstance(raw, dict) or "param" not in raw:
            return None
        try:
            param = int(raw["param"])
        except (TypeError, ValueError):
            return None
        if param < 0:
            return None
        return cls(
            param=param,
            name=str(raw.get("name", "")),
            value=_clamp(raw.get("value", 0.0)),
            default=_clamp(raw.get("default", 0.0)),
            mode="momentary" if raw.get("mode") == "momentary" else "toggle",
            taper=raw["taper"] if raw.get("taper") in ("log", "exp") else "linear",
            sensitivity=_span(raw.get("sensitivity", 1.0)),
        )


@dataclass(frozen=True)
class Reserved:
    kind: str                      # "builtin" | "action"
    id: str

    @classmethod
    def parse(cls, raw) -> "Reserved | None":
        if not isinstance(raw, dict):
            return None
        kind, ident = raw.get("kind"), raw.get("id")
        if kind not in ("builtin", "action") or not ident:
            return None
        return cls(kind=kind, id=str(ident))


@dataclass(frozen=True)
class Track:
    index: int = -1
    name: str = ""
    volume: float = 0.0


# How the fader behaves. The panel owns these -- it is where they are edited,
# and the project's rule is that the panel is the single authority -- so they
# arrive here in state.json rather than in config.toml. The defaults below are
# what an older panel that publishes no "fader" block gets, and they are the
# behaviour this bridge had before the modes existed.
ABSOLUTE, PICKUP, RELATIVE = "absolute", "pickup", "relative"
FADER_MODES = (ABSOLUTE, PICKUP, RELATIVE)


@dataclass(frozen=True)
class Fader:
    mode: str = PICKUP
    # Relative only: one raw fader step (1/127) moves the volume by this much
    # of the range, times this. 1.0 is full travel = full range.
    sensitivity: float = 1.0
    # Relative only: how many raw counts at each end of the travel count as
    # "out of road". Entering one suspends output until the fader is walked
    # back -- see Engine._on_fader_relative.
    end_zone: int = 3
    # Relative only: a recalibration move that stops for this long is over.
    recal_timeout: float = 1.0

    @classmethod
    def parse(cls, raw) -> "Fader":
        raw = raw if isinstance(raw, dict) else {}
        mode = raw.get("mode")
        return cls(
            mode=mode if mode in FADER_MODES else PICKUP,
            sensitivity=_span(raw.get("sensitivity", 1.0)),
            # An end zone of 0 disables the pause entirely, which is a
            # legitimate thing to want; a huge one would leave no usable
            # travel, so cap it well short of half the range.
            end_zone=min(32, max(0, _int(raw.get("end_zone"), 3))),
            recal_timeout=_clamp(raw.get("recal_timeout", 1.0), 0.05, 10.0),
        )


@dataclass(frozen=True)
class Fx:
    index: int = -1
    ident: str = ""
    name: str = ""
    # The track the focused plugin is ON, which is not necessarily the
    # selected track in Track above -- that one belongs to the fader.
    track: int = -1


@dataclass(frozen=True)
class Notify:
    """Command IDs the daemon fires to tell the panel something."""
    layer_a: str = ""
    layer_b: str = ""

    def for_layer(self, layer: str) -> str:
        return self.layer_a if layer == "A" else self.layer_b


@dataclass(frozen=True)
class State:
    seq: int = -1
    active: bool = False
    track: Track = field(default_factory=Track)
    fx: Fx = field(default_factory=Fx)
    # layer -> ("encoders"|"buttons") -> list of Slot|None
    layers: dict = field(default_factory=dict)
    reserved: tuple = ()
    notify: Notify = field(default_factory=Notify)
    fader: Fader = field(default_factory=Fader)

    def slot(self, layer: str, kind: str, index: int) -> Slot | None:
        """The mapping for one physical control, or None if unassigned."""
        if not self.active:
            return None
        try:
            return self.layers[layer][kind][index]
        except (KeyError, IndexError):
            return None

    def reserved_at(self, index: int) -> Reserved | None:
        return self.reserved[index] if 0 <= index < len(self.reserved) else None


def _obj(value) -> dict:
    """A dict, or an empty one. Never raises on the wrong shape."""
    return value if isinstance(value, dict) else {}


def _int(value, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _span(value, low: float = 0.1, high: float = 8.0) -> float:
    """A multiplier, defaulting to 1.0 rather than to the low bound."""
    try:
        return min(high, max(low, float(value)))
    except (TypeError, ValueError):
        return 1.0


def _clamp(value, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return min(high, max(low, float(value)))
    except (TypeError, ValueError):
        return low


def _slot_list(raw, count: int) -> list:
    items = raw if isinstance(raw, list) else []
    out = [Slot.parse(items[i]) if i < len(items) else None for i in range(count)]
    return out


def parse(text: str) -> State:
    """Parse state.json text. Malformed input yields an inactive state.

    Deliberately total: a panel mid-rewrite or an older schema must leave the
    controller inert rather than driving whatever parameter index happens to
    survive the damage.
    """
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return State()
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return State()

    # Every container is type-checked, not just truthiness-checked: a field
    # that arrives as a string rather than an object must leave the controller
    # inert, not raise out of the daemon's poll loop.
    track_raw = _obj(raw.get("track"))
    fx_raw = _obj(raw.get("fx"))
    layers_raw = _obj(raw.get("layers"))
    layers = {}
    for layer in LAYERS:
        block = _obj(layers_raw.get(layer))
        layers[layer] = {
            "encoders": _slot_list(block.get("encoders"), ENCODERS),
            "buttons": _slot_list(block.get("buttons"), BUTTONS),
        }

    reserved_raw = raw.get("reserved")
    reserved_raw = reserved_raw if isinstance(reserved_raw, list) else []
    reserved = tuple(
        Reserved.parse(reserved_raw[i]) if i < len(reserved_raw) else None
        for i in range(RESERVED)
    )

    notify_raw = _obj(raw.get("notify"))

    def _cmd(key):
        value = notify_raw.get(key)
        return str(value) if isinstance(value, str) else ""

    return State(
        seq=_int(raw.get("seq")),
        active=bool(raw.get("active")) and _int(fx_raw.get("index")) >= 0,
        track=Track(
            index=_int(track_raw.get("index")),
            name=str(track_raw.get("name", "")),
            volume=_clamp(track_raw.get("volume", 0.0)),
        ),
        fx=Fx(
            index=_int(fx_raw.get("index")),
            ident=str(fx_raw.get("ident", "")),
            name=str(fx_raw.get("name", "")),
            track=_int(fx_raw.get("track")),
        ),
        layers=layers,
        reserved=reserved,
        notify=Notify(layer_a=_cmd("layer_a"), layer_b=_cmd("layer_b")),
        fader=Fader.parse(raw.get("fader")),
    )


class StateFile:
    """Polls state.json and hands out the latest parse."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.state = State()
        self._stamp: tuple | None = None

    def poll(self) -> bool:
        """Re-read if the file changed. True if the state was replaced."""
        try:
            st = os.stat(self.path)
        except OSError:
            if self._stamp is None:
                return False
            # The file went away: stop driving anything.
            self._stamp, self.state = None, State()
            return True

        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return False
        try:
            text = self.path.read_text()
        except OSError:
            return False

        self._stamp = stamp
        new = parse(text)
        # seq guards the case of two writes inside one mtime granule: if the
        # panel says nothing moved, keep what we have rather than churning.
        if new.seq >= 0 and new.seq == self.state.seq and self.state.active:
            return False
        self.state = new
        return True

    def note_value(self, layer: str, kind: str, index: int, value: float) -> None:
        """Record a value we or REAPER just changed, so the shadow stays true."""
        slot = self.state.slot(layer, kind, index)
        if slot is None:
            return
        self.state.layers[layer][kind][index] = replace(slot, value=_clamp(value))
