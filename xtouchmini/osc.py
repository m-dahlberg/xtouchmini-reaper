"""OSC encode/decode over UDP.

Hand-rolled rather than a python-osc dependency, for the same reason as the
ShuttleXpress bridge: a message with one float argument is a dozen lines, and
this system's Python has no pip.

Unlike that bridge this one is bidirectional -- REAPER sends parameter feedback
back to us to drive the LED rings -- so there is a decoder here as well, and it
has to understand bundles: REAPER batches feedback into "#bundle" packets and a
decoder that only understood bare messages would silently see nothing at all
during the burst that follows an FX focus change.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

BUNDLE_TAG = b"#bundle\0"


def _pad(data: bytes) -> bytes:
    """OSC strings are null-terminated and padded to a 4-byte boundary."""
    data += b"\0"
    return data + b"\0" * (-len(data) % 4)


def encode(address: str, *args: float | int | str) -> bytes:
    """One OSC message. Floats, 32-bit ints and strings are supported."""
    tags = b","
    body = b""
    for arg in args:
        if isinstance(arg, bool):
            raise TypeError("OSC has no bool type; send an int or a float")
        if isinstance(arg, str):
            tags += b"s"
            body += _pad(arg.encode())
        elif isinstance(arg, int):
            tags += b"i"
            body += struct.pack(">i", arg)
        else:
            tags += b"f"
            body += struct.pack(">f", float(arg))
    return _pad(address.encode()) + _pad(tags) + body


def _advance(pos: int, end: int) -> int:
    length = end - pos + 1
    return pos + length + (-length % 4)


def _string_at(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\0", pos)
    return data[pos:end].decode("utf-8", "replace"), _advance(pos, end)


@dataclass(frozen=True)
class Message:
    address: str
    args: tuple

    def __repr__(self) -> str:
        return f"{self.address} {' '.join(repr(a) for a in self.args)}".strip()


def decode(packet: bytes) -> list[Message]:
    """Flatten one UDP packet into its messages. Bundles may nest."""
    if packet.startswith(BUNDLE_TAG):
        out: list[Message] = []
        pos = len(BUNDLE_TAG) + 8  # skip the time tag; we act immediately
        while pos + 4 <= len(packet):
            (size,) = struct.unpack_from(">i", packet, pos)
            pos += 4
            if size < 0 or pos + size > len(packet):
                break
            out.extend(decode(packet[pos:pos + size]))
            pos += size
        return out

    try:
        address, pos = _string_at(packet, 0)
        if not address.startswith("/"):
            return []
        tags, pos = _string_at(packet, pos)
    except ValueError:
        return []
    if not tags.startswith(","):
        return []

    args: list = []
    for tag in tags[1:]:
        try:
            if tag == "f":
                args.append(struct.unpack_from(">f", packet, pos)[0])
                pos += 4
            elif tag == "i":
                args.append(struct.unpack_from(">i", packet, pos)[0])
                pos += 4
            elif tag == "s":
                text, pos = _string_at(packet, pos)
                args.append(text)
            elif tag in "TF":
                args.append(tag == "T")
            elif tag == "N":
                args.append(None)
            else:
                # An unknown tag means every following offset is guesswork.
                break
        except (struct.error, ValueError):
            # Truncated payload. Keep the arguments that did parse and stop --
            # a datagram can be cut short, and a decoder that raised here
            # would take the daemon's whole feedback loop down with it.
            break
    return [Message(address, tuple(args))]


class Client:
    """Fire-and-forget UDP sender."""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self._sock: socket.socket | None = None

    def open(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def send(self, address: str, *args: float | int | str) -> None:
        if self._sock is None:
            return
        self._sock.sendto(encode(address, *args), (self.host, self.port))


class Server:
    """Non-blocking UDP receiver, driven by the daemon's poll loop."""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self._sock: socket.socket | None = None

    def open(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.setblocking(False)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def fileno(self) -> int:
        return -1 if self._sock is None else self._sock.fileno()

    def read(self) -> list[Message]:
        """Drain every datagram waiting right now."""
        if self._sock is None:
            return []
        out: list[Message] = []
        while True:
            try:
                packet, _addr = self._sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return out
            except OSError:
                return out
            out.extend(decode(packet))
