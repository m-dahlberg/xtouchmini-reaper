"""xtouchmini -- X-Touch Mini to REAPER bridge.

    xtouchmini run       (default) the daemon
    xtouchmini monitor   decode controller events to stdout, touch nothing
    xtouchmini ports     list MIDI ports and say which one matches
    xtouchmini leds      light everything briefly, to prove the output path
    xtouchmini mode std|mc   switch the device's operating mode

The device is waited for rather than required: it can be unplugged and plugged
back in at any time. The OSC sockets are opened once, outside that loop, so
REAPER never sees the surface come and go.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from . import osc as osc_mod
from . import protocol as P
from .config import Config, load
from .device import Device, DeviceGone, find
from .engine import Engine
from .state import StateFile

POLL_SECONDS = 0.004        # worst-case added latency on a knob turn
STALE_CHECK_SECONDS = 5.0   # how often to notice our own source changing
IDLE_POLL_SECONDS = 0.25    # while waiting for the device to appear
# How often to ask ALSA whether the device is still attached. An unplug
# raises nothing -- see Device.alive -- so this is the only thing that
# notices one, and the whole reconnect path hangs off it.
LIVE_CHECK_SECONDS = 1.0


class Stopped(Exception):
    """SIGINT/SIGTERM, turned into something the main loop can unwind."""


def _install_signal_handlers() -> None:
    def stop(_sig, _frame):
        raise Stopped()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


def _source_mtime() -> float:
    """Newest mtime among our own modules."""
    here = os.path.dirname(os.path.abspath(__file__))
    newest = 0.0
    for root, _dirs, files in os.walk(here):
        for name in files:
            if name.endswith(".py"):
                try:
                    newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
                except OSError:
                    pass
    return newest


def _log(*args) -> None:
    # stderr only, no logging module: this runs under systemd and the journal
    # is the log. Same choice as the ShuttleXpress bridge.
    print(*args, file=sys.stderr, flush=True)


def _wait_for_device(device: Device, interval: float) -> None:
    announced = False
    while True:
        if device.try_open():
            _log(f"connected: {device.name}")
            return
        if not announced:
            if find(device.port_match) is None:
                _log(f"waiting for a MIDI port matching "
                     f"{device.port_match!r}...")
            else:
                _log(f"{device.port_match!r} is present but will not open -- "
                     f"REAPER claims it exclusively when the X-Touch is "
                     f"enabled in Preferences > Audio > MIDI Devices.")
            announced = True
        time.sleep(interval)


def cmd_run(cfg: Config) -> int:
    client = osc_mod.Client(cfg.osc.host, cfg.osc.send_port)
    server = osc_mod.Server(cfg.osc.listen_host, cfg.osc.listen_port)
    client.open()
    try:
        server.open()
    except OSError as exc:
        _log(f"cannot listen on {cfg.osc.listen_host}:{cfg.osc.listen_port}: "
             f"{exc}")
        return 1
    _log(f"OSC: sending to {cfg.osc.host}:{cfg.osc.send_port}, "
         f"listening on {cfg.osc.listen_port}")

    device = Device(cfg.device.port_match)
    state = StateFile(cfg.state_path)
    engine = Engine(client, device, cfg.feel)
    encoding = cfg.encoding
    _log(f"encoder encoding: {encoding.value}")
    if os.environ.get("XTOUCHMINI_DEBUG"):
        # Every decoded event to the journal. The single most useful thing when
        # an encoder "moves both ways": it shows the raw delta the hardware
        # actually sent, separately from what the parameter then did.
        engine.set_debug(lambda event: _log(f"  {event}"))
        _log("debug: logging every decoded event")
    if os.environ.get("XTOUCHMINI_DEBUG_FADER"):
        # Quiet next to DEBUG_OSC -- a dozen lines for a whole fader sweep --
        # and it is the only switch that reports the fader DECLINING to do
        # something, which is the half of its behaviour that has no other
        # outward sign.
        engine.set_fader_debug(lambda text: _log(f"  {text}"))
        _log("debug: logging every fader decision")
    if os.environ.get("XTOUCHMINI_DEBUG_OSC"):
        # Much louder: an FX focus change alone is several hundred messages.
        # This is the one that answers "which plugin did REAPER say that was
        # about?", which is the question behind every LED that shows the wrong
        # parameter.
        engine.set_osc_debug(lambda text: _log(f"  {text}"))
        _log("debug: logging every inbound OSC message")
    if not P.TX_LAYER_B.measured:
        _log("note: layer B numbers are a factory-default guess, not measured")

    try:
        while True:
            _wait_for_device(device, cfg.device.reconnect_interval)
            # A freshly attached device has all its lamps off, whatever the
            # cache thinks it last sent.
            engine._forget_leds()
            engine.refresh_leds()
            try:
                _pump(device, server, state, engine, encoding)
            except DeviceGone as exc:
                _log(f"device went away: {exc}")
                device.close()
    except Stopped:
        _log("stopping")
    finally:
        try:
            if device.open:
                engine.all_off()
        except DeviceGone:
            pass
        device.close()
        server.close()
        client.close()
    return 0


def _pump(device, server, state, engine, encoding) -> None:
    import select
    # The checkout is the installation, so editing the source does nothing
    # until the service is restarted. Say so rather than letting a change
    # appear to have had no effect -- which is exactly how an afternoon gets
    # spent debugging code that is not running.
    started_with = _source_mtime()
    next_check = time.monotonic() + STALE_CHECK_SECONDS
    next_live = time.monotonic() + LIVE_CHECK_SECONDS
    warned = False

    while True:
        if state.poll():
            engine.set_state(state.state)

        now = time.monotonic()
        if not warned and now >= next_check:
            next_check = now + STALE_CHECK_SECONDS
            if _source_mtime() > started_with:
                _log("NOTE: the daemon source has changed since this process "
                     "started -- run `systemctl --user restart xtouchmini` "
                     "for it to take effect")
                warned = True

        if now >= next_live:
            next_live = now + LIVE_CHECK_SECONDS
            if not device.alive():
                raise DeviceGone(f"{device.name!r} is gone from ALSA")

        ready, _, _ = select.select([server.fileno()], [], [], POLL_SECONDS)
        if ready:
            engine.on_feedback(server.read())

        for msg in device.read():
            event = P.decode(msg, encoding)
            if event is not None:
                engine.handle(event)

        engine.tick()


def cmd_monitor(cfg: Config) -> int:
    device = Device(cfg.device.port_match)
    _wait_for_device(device, cfg.device.reconnect_interval)
    _log("monitoring; nothing is sent to REAPER. Ctrl-C to stop.")
    try:
        while True:
            for msg in device.read():
                raw = " ".join(f"{b:02X}" for b in msg)
                event = P.decode(msg, cfg.encoding)
                print(f"{raw:12s} {event}", flush=True)
            time.sleep(POLL_SECONDS)
    except Stopped:
        pass
    finally:
        device.close()
    return 0


def cmd_ports(cfg: Config) -> int:
    import rtmidi
    for label, cls in (("in ", rtmidi.MidiIn), ("out", rtmidi.MidiOut)):
        for i, name in enumerate(cls().get_ports()):
            mark = ("  <-- match" if cfg.device.port_match.upper() in name.upper()
                    else "")
            print(f"{label} {i}: {name}{mark}")
    if find(cfg.device.port_match) is None:
        print(f"\nnothing matches {cfg.device.port_match!r}")
        return 1
    return 0


def cmd_leds(cfg: Config) -> int:
    device = Device(cfg.device.port_match)
    if not device.try_open():
        _log("cannot open the device")
        return 1
    # Both layers, because the rings are addressed by different CCs on each and
    # this command exists to prove the output path whichever layer the device
    # happens to be showing. The button lamps are shared.
    layers = (P.LAYER_A, P.LAYER_B)
    try:
        for layer in layers:
            for i in range(P.ENCODERS):
                device.send(P.ring_all(i, True, layer))
        for i in range(P.BUTTONS):
            device.send(P.button_led(i, P.LedState.ON))
        time.sleep(1.5)
        for pos in range(1, P.RING_LEDS + 1):
            for layer in layers:
                for i in range(P.ENCODERS):
                    device.send(P.ring_position(
                        i, (pos - 1) / (P.RING_LEDS - 1), layer))
            time.sleep(0.08)
    finally:
        for layer in layers:
            for i in range(P.ENCODERS):
                device.send(P.ring_all(i, False, layer))
        for i in range(P.BUTTONS):
            device.send(P.button_led(i, P.LedState.OFF))
        device.close()
    return 0


def cmd_mode(cfg: Config, mode: str) -> int:
    device = Device(cfg.device.port_match)
    if not device.try_open():
        _log("cannot open the device")
        return 1
    try:
        device.send(P.select_mode(mc=(mode == "mc")))
        _log(f"switched to {'MC' if mode == 'mc' else 'Standard'} mode; "
             f"the setting is remembered across power-off")
    finally:
        device.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="xtouchmini")
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run")
    sub.add_parser("monitor")
    sub.add_parser("ports")
    sub.add_parser("leds")
    mode = sub.add_parser("mode")
    mode.add_argument("mode", choices=["std", "mc"])
    args = parser.parse_args(argv)

    _install_signal_handlers()
    try:
        cfg = load(args.config)
    except ValueError as exc:
        _log(f"config error: {exc}")
        return 2

    try:
        if args.command == "monitor":
            return cmd_monitor(cfg)
        if args.command == "ports":
            return cmd_ports(cfg)
        if args.command == "leds":
            return cmd_leds(cfg)
        if args.command == "mode":
            return cmd_mode(cfg, args.mode)
        return cmd_run(cfg)
    except Stopped:
        return 0


if __name__ == "__main__":
    sys.exit(main())
