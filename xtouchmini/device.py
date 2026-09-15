"""The X-Touch Mini, over ALSA.

python-rtmidi rather than raw /dev/snd/midiC*: the port name is the only
unambiguous handle on this machine. The ALSA card ids are "MINI" for the
X-Touch and "Mini" for a RODE microphone -- a case-insensitive match on the
card id would open the wrong device, and the card *number* changes with the
order things are plugged in.

REAPER claims this device exclusively when it is enabled in its MIDI prefs, so
it must be disabled there for the daemon to open it at all.
"""

from __future__ import annotations

import rtmidi


class DeviceGone(OSError):
    """The device disappeared; the caller should go back to waiting for it."""


def _match(ports: list[str], needle: str) -> int | None:
    needle = needle.upper()
    for i, name in enumerate(ports):
        if needle in name.upper():
            return i
    return None


def find(port_match: str) -> str | None:
    """The full name of the matching input port, or None."""
    ports = rtmidi.MidiIn().get_ports()
    index = _match(ports, port_match)
    return ports[index] if index is not None else None


class Device:
    def __init__(self, port_match: str):
        self.port_match = port_match
        self._in: rtmidi.MidiIn | None = None
        self._out: rtmidi.MidiOut | None = None
        self.name = ""

    @property
    def open(self) -> bool:
        return self._in is not None

    def try_open(self) -> bool:
        """Open both directions, or leave everything closed. Never raises."""
        midi_in, midi_out = rtmidi.MidiIn(), rtmidi.MidiOut()
        in_index = _match(midi_in.get_ports(), self.port_match)
        out_index = _match(midi_out.get_ports(), self.port_match)
        if in_index is None or out_index is None:
            return False
        try:
            midi_in.open_port(in_index)
            midi_out.open_port(out_index)
        except (rtmidi.SystemError, rtmidi.InvalidPortError, OSError):
            # Almost always REAPER holding the device: it takes ALSA rawmidi
            # exclusively when the X-Touch is enabled in its MIDI prefs.
            return False
        midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        self._in, self._out = midi_in, midi_out
        self.name = midi_in.get_ports()[in_index]
        return True

    def close(self) -> None:
        for port in (self._in, self._out):
            if port is not None:
                try:
                    port.close_port()
                except Exception:       # noqa: BLE001 -- closing must not throw
                    pass
        self._in = self._out = None
        self.name = ""

    def alive(self) -> bool:
        """False once the port this handle was opened on has gone away.

        An ALSA unplug does not make rtmidi raise. The sequencer port is torn
        down, the subscription goes with it, and get_message() simply returns
        None for ever after -- indistinguishable, from inside read(), from a
        controller nobody is touching. So the daemon cannot wait to be told:
        it has to ask ALSA whether the port is still there.

        Asking by name is enough because the question is only ever answered
        during the unplugged window, which is as long as the cable is out. A
        replug puts the name back, and often the same client number, so
        comparing names across one would see nothing -- but by then the caller
        has already dropped the stale handle and gone back to waiting.
        """
        if self._in is None:
            return False
        return self.name in rtmidi.MidiIn().get_ports()

    def read(self) -> list[list[int]]:
        """Every message waiting right now."""
        if self._in is None:
            return []
        out = []
        while True:
            try:
                item = self._in.get_message()
            except (rtmidi.SystemError, OSError) as exc:
                raise DeviceGone(str(exc)) from exc
            if item is None:
                return out
            out.append(list(item[0]))

    def send(self, message: list[int]) -> None:
        if self._out is None:
            return
        try:
            self._out.send_message(message)
        except (rtmidi.SystemError, OSError) as exc:
            raise DeviceGone(str(exc)) from exc
