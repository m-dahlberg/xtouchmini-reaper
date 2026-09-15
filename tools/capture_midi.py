#!/usr/bin/env python3
"""Capture and probe the X-Touch Mini MIDI protocol.

Task 1 of the build plan: every CC/note number used by the daemon comes from
here, measured, rather than from the manual. REAPER must not have the device
open -- it claims ALSA rawmidi exclusively (Preferences -> Audio -> MIDI
Devices -> X-TOUCH MINI -> disable input and output).

Port matching is by name. The ALSA card ids collide case-insensitively on this
machine ("Mini" is a RODE NT-USB Mini, "MINI" is the X-Touch), so never match
on card id or index.

Usage:
    capture_midi.py ports
    capture_midi.py dump [seconds]
    capture_midi.py session [--max S] [--quiet S] [--log PATH]
    capture_midi.py analyze <logfile>
    capture_midi.py send <byte> [byte ...]      # decimal or 0x hex
    capture_midi.py sweep cc <first> <last> [--ch N] [--val V] [--delay S]
    capture_midi.py sweep note <first> <last> [--ch N] [--vel V] [--delay S]
    capture_midi.py ring <cc> [--ch N] [--delay S]   # walk one ring 0..127
"""
import sys
import time

import rtmidi

PORT_MATCH = "X-TOUCH MINI"

CC, NOTE_ON, NOTE_OFF, PITCH = 0xB0, 0x90, 0x80, 0xE0


def _find(port_list, kind):
    for i, name in enumerate(port_list.get_ports()):
        if PORT_MATCH in name.upper():
            return i, name
    sys.exit(f"no {kind} port matching {PORT_MATCH!r}; "
             f"saw {port_list.get_ports()}")


def open_in():
    m = rtmidi.MidiIn()
    idx, name = _find(m, "input")
    m.open_port(idx)
    m.ignore_types(sysex=False, timing=True, active_sense=True)
    return m, name


def open_out():
    m = rtmidi.MidiOut()
    idx, name = _find(m, "output")
    m.open_port(idx)
    return m, name


def describe(msg):
    """Human-readable decode, with the raw bytes always shown."""
    raw = " ".join(f"{b:02X}" for b in msg)
    if not msg:
        return raw
    status, ch = msg[0] & 0xF0, (msg[0] & 0x0F) + 1
    if status == CC and len(msg) >= 3:
        cc, val = msg[1], msg[2]
        # relative two's-complement reads as a small +/- around 0 or 64
        rel = ""
        if val < 8:
            rel = f"  (rel +{val})"
        elif 64 < val < 72:
            rel = f"  (rel -{val - 64})"
        return f"{raw}   CC  ch{ch:<2} cc={cc:<3} val={val:<3}{rel}"
    if status == NOTE_ON and len(msg) >= 3:
        kind = "note-on " if msg[2] else "note-off"
        return f"{raw}   {kind} ch{ch:<2} note={msg[1]:<3} vel={msg[2]}"
    if status == NOTE_OFF and len(msg) >= 3:
        return f"{raw}   note-off ch{ch:<2} note={msg[1]:<3} vel={msg[2]}"
    if status == PITCH and len(msg) >= 3:
        bend = (msg[2] << 7) | msg[1]
        return f"{raw}   pitchbend ch{ch:<2} value={bend} (0-16383)"
    if msg[0] == 0xF0:
        return f"{raw}   SYSEX ({len(msg)} bytes)"
    return raw


def cmd_ports():
    for kind, cls in (("in ", rtmidi.MidiIn), ("out", rtmidi.MidiOut)):
        for i, name in enumerate(cls().get_ports()):
            mark = " <-- match" if PORT_MATCH in name.upper() else ""
            print(f"{kind} {i}: {name}{mark}")


def cmd_dump(seconds):
    midi, name = open_in()
    print(f"listening on {name!r} for {seconds}s -- touch every control")
    print("(each row: raw bytes, then decode)\n")
    start = time.time()
    seen = set()
    try:
        while time.time() - start < seconds:
            item = midi.get_message()
            if item is None:
                time.sleep(0.001)
                continue
            msg, _delta = item
            # flush: stdout is block-buffered when redirected to a file, and a
            # capture you cannot watch live is a capture you cannot tell is
            # working until the three minutes are already gone.
            print(f"{time.time() - start:7.3f}  {describe(msg)}", flush=True)
            if len(msg) >= 2:
                seen.add((msg[0] & 0xF0, msg[0] & 0x0F, msg[1]))
    finally:
        midi.close_port()
    print(f"\n{len(seen)} distinct (status, channel, data1) triples seen")


def _parse_byte(tok):
    return int(tok, 16) if tok.lower().startswith("0x") else int(tok)


def cmd_send(tokens):
    midi, name = open_out()
    msg = [_parse_byte(t) for t in tokens]
    print(f"-> {name}: {' '.join(f'{b:02X}' for b in msg)}")
    midi.send_message(msg)
    time.sleep(0.05)
    midi.close_port()


def cmd_sweep(kind, first, last, channel, value, delay):
    midi, name = open_out()
    status = (CC if kind == "cc" else NOTE_ON) | (channel - 1)
    print(f"-> {name}: {kind} {first}..{last} ch{channel} value={value}, "
          f"{delay}s apart -- watch the hardware")
    try:
        for n in range(first, last + 1):
            print(f"  {kind}={n}")
            midi.send_message([status, n, value])
            time.sleep(delay)
    finally:
        # leave nothing lit
        for n in range(first, last + 1):
            midi.send_message([status, n, 0])
        midi.close_port()


def cmd_ring(cc, channel, delay):
    midi, name = open_out()
    status = CC | (channel - 1)
    print(f"-> {name}: walking cc={cc} ch{channel} through 0..127")
    try:
        for v in range(128):
            print(f"  val={v}")
            midi.send_message([status, cc, v])
            time.sleep(delay)
    finally:
        midi.send_message([status, cc, 0])
        midi.close_port()


def summarize(events):
    """Infer the protocol from what was captured.

    The question that decides the whole design is whether the encoders send
    relative deltas or absolute positions, and that is visible in the spread of
    values on each CC: a relative encoder only ever emits a handful of small
    numbers clustered near 0 and 64, while an absolute one sweeps 0..127.
    """
    by_key = {}
    for status, ch, d1, d2 in events:
        by_key.setdefault((status, ch, d1), []).append(d2)

    lines = []
    for (status, ch, d1), values in sorted(by_key.items()):
        lo, hi = min(values), max(values)
        distinct = sorted(set(values))
        if status == CC:
            small = [v for v in distinct if v < 16 or 64 <= v < 80]
            if len(distinct) <= 8 and len(small) == len(distinct) and hi <= 80:
                verdict = "RELATIVE (two's complement)"
            elif len(distinct) > 16 and hi - lo > 60:
                verdict = "ABSOLUTE (sweeps its range)"
            else:
                verdict = "unclear -- needs a longer turn"
            lines.append(f"  CC   ch{ch:<2} cc={d1:<3} n={len(values):<4} "
                         f"values {lo}..{hi} ({len(distinct)} distinct)  {verdict}")
        elif status == NOTE_ON:
            ons = sum(1 for v in values if v)
            lines.append(f"  NOTE ch{ch:<2} note={d1:<3} n={len(values):<4} "
                         f"{ons} on / {len(values) - ons} off   velocities {distinct}")
        elif status == PITCH:
            lines.append(f"  PB   ch{ch:<2} n={len(values):<4} (LSB only shown)")
        else:
            lines.append(f"  {status:#04x} ch{ch:<2} d1={d1:<3} n={len(values)}")
    return lines


def cmd_session(max_s, quiet_s, path):
    """Listen until the controls go quiet, then report. Arm it and walk away."""
    midi, name = open_in()
    log = open(path, "w") if path else None
    print(f"armed on {name!r} -- up to {max_s:.0f}s, "
          f"stops {quiet_s:.0f}s after the last message")
    started = time.time()
    last_msg = None
    events = []
    try:
        while True:
            now = time.time()
            if last_msg is None:
                if now - started > max_s:
                    print("\nwindow closed with nothing received")
                    break
            elif now - last_msg > quiet_s:
                break

            item = midi.get_message()
            if item is None:
                time.sleep(0.002)
                continue
            msg, _delta = item
            last_msg = time.time()
            row = f"{last_msg - started:7.3f}  {describe(msg)}"
            print(row, flush=True)
            if log:
                log.write(row + "\n")
                log.flush()
            if len(msg) >= 3:
                events.append((msg[0] & 0xF0, (msg[0] & 0x0F) + 1, msg[1], msg[2]))
    finally:
        midi.close_port()
        if log:
            log.close()

    if events:
        print(f"\n=== {len(events)} messages, grouped ===")
        for line in summarize(events):
            print(line)
    return 0


def cmd_analyze(path):
    """Re-run the inference over a saved raw log."""
    events = []
    for line in open(path):
        raw = line.split("  ", 1)
        if len(raw) < 2:
            continue
        parts = raw[1].split()
        try:
            data = [int(b, 16) for b in parts[:3]]
        except ValueError:
            continue
        if len(data) == 3:
            events.append((data[0] & 0xF0, (data[0] & 0x0F) + 1, data[1], data[2]))
    print(f"=== {len(events)} messages from {path} ===")
    for line in summarize(events):
        print(line)
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]

    def opt(flag, default, cast=int):
        if flag in rest:
            i = rest.index(flag)
            v = cast(rest[i + 1])
            del rest[i:i + 2]
            return v
        return default

    if cmd == "ports":
        cmd_ports()
    elif cmd == "dump":
        cmd_dump(float(rest[0]) if rest else 20.0)
    elif cmd == "send":
        cmd_send(rest)
    elif cmd == "sweep":
        kind = rest.pop(0)
        ch = opt("--ch", 1)
        val = opt("--val", 127) if kind == "cc" else opt("--vel", 127)
        delay = opt("--delay", 0.4, float)
        cmd_sweep(kind, int(rest[0]), int(rest[1]), ch, val, delay)
    elif cmd == "session":
        cmd_session(opt("--max", 3600.0, float), opt("--quiet", 20.0, float),
                    opt("--log", "docs/capture-raw.log", str))
    elif cmd == "analyze":
        cmd_analyze(rest[0])
    elif cmd == "ring":
        ch = opt("--ch", 1)
        delay = opt("--delay", 0.05, float)
        cmd_ring(int(rest[0]), ch, delay)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
