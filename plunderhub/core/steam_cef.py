# -*- coding: utf-8 -*-
#
# PlunderHub - a unified game launcher for Linux
# Copyright (C) 2026 Ikraja
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Steam's downloads, steered from the Loading Dock: pause, resume, order.

Steam has no command for them; its own Downloads page drives them through
the client's internal SteamClient.Downloads API (what Decky Loader uses on
the Steam Deck). That page's JavaScript can be reached only through the
client's Chromium remote-debugging port, which Steam opens (127.0.0.1:8080,
this computer only) when an empty .cef-enable-remote-debugging file sits in
its folder at start. Opt-in - off by default: while it's open, programs on
this computer could steer Steam through it. The Loading Dock switches it on
and, once its Steam installs are done, off again (the file goes; the port
closes when Steam next starts).

The API's names: BossSloth/SteamTypes, src/types/SteamClient/Downloads.ts.
The connection is a few lines of WebSocket on the standard library - no
extra package on any distribution."""

import base64
import json
import os
import socket
import struct
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from plunderhub.logging_setup import logger

FLAG = ".cef-enable-remote-debugging"
PORT = 8080
TIMEOUT_S = 4.0


def _roots() -> List[Path]:
    from plunderhub.core.steam_heroic import steam_roots
    return list(steam_roots())


def enabled() -> bool:
    return any((r / FLAG).exists() for r in _roots())


def enable() -> bool:
    """The flag in every Steam install (native, Flatpak, Snap); Steam reads it at start."""
    ok = False
    for r in _roots():
        try:
            (r / FLAG).touch(exist_ok=True)
            ok = True
        except OSError as e:
            logger.warning(f"Steam control: {r / FLAG} not written: {e}")
    logger.info(f"Steam control: switched on ({'ok' if ok else 'failed'})")
    return ok


def disable():
    for r in _roots():
        try:
            (r / FLAG).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"Steam control: {r / FLAG} not removed: {e}")
    logger.info("Steam control: switched off (the port closes when Steam next starts)")


def _target() -> Optional[str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=TIMEOUT_S) as r:
            targets = json.loads(r.read(1 << 20))
    except (OSError, ValueError):
        return None
    for t in targets if isinstance(targets, list) else []:
        if isinstance(t, dict) and t.get("title") == "SharedJSContext" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    return None


def available() -> bool:
    """Steam is up with the port open (it was started after the switch)."""
    return _target() is not None


class _WebSocket:
    """Just enough RFC 6455 for one request and its answer."""

    def __init__(self, url: str):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=TIMEOUT_S)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                          .encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("Steam closed the connection")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise OSError(head.split(b"\r\n", 1)[0].decode("latin-1"))
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def send(self, text: str):
        data, mask = text.encode(), os.urandom(4)
        n = len(data)
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        elif n < 65536:
            head = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("Steam closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> str:
        msg = b""
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            op = b1 & 0x0F
            if op == 8:
                raise OSError("Steam closed the connection")
            if op in (0, 1, 2):
                msg += payload
                if b1 & 0x80:
                    return msg.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))     # a clean close frame
        except OSError:
            pass
        self.sock.close()


def evaluate(expression: str):
    """Runs JavaScript in Steam's SharedJSContext; its value, or None.
    Blocks up to a few seconds - a worker thread."""
    url = _target()
    if url is None:
        return None
    ws = None
    try:
        ws = _WebSocket(url)
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expression, "returnByValue": True}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                result = (msg.get("result") or {}).get("result") or {}
                return result.get("value")
    except (OSError, ValueError) as e:
        logger.debug(f"Steam control: {e}")
        return None
    finally:
        if ws is not None:
            ws.close()


_STATE_JS = """(() => {
  const s = window.downloadsStore; if (!s) return null;
  const o = s.LocalDownloadOverview || s.DownloadOverview || {};
  const pick = d => ({appid: d.appid, active: !!d.active, paused: !!d.paused, completed: !!d.completed,
                      queue: d.queue_index, got: d.downloaded_bytes || 0, total: d.total_bytes || 0});
  return JSON.stringify({paused: !!o.paused, appid: o.update_appid || 0,
    rate: o.update_network_bytes_per_second || 0, left: o.update_seconds_remaining || 0,
    queued: (s.QueuedTransfers || []).map(pick), unqueued: (s.UnqueuedTransfers || []).map(pick)});
})()"""


def downloads() -> Optional[Dict]:
    """{paused, appid (the one downloading), rate, left, queued: [...],
    unqueued: [...]} as Steam's Downloads page knows them; None without
    the port."""
    raw = evaluate(_STATE_JS)
    try:
        return json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _call(js: str) -> bool:
    # every call takes the client it's for as its last argument - Steam's own
    # Downloads page passes downloadsStore.CurrentViewingRemoteClientID
    # (steamui/sp.js); without it the call is accepted and does nothing
    body = f"const cid = window.downloadsStore ? downloadsStore.CurrentViewingRemoteClientID : undefined; {js}"
    ok = evaluate(f"(() => {{ {body}; return true; }})()") is True
    logger.info(f"Steam control: {js} ({'ok' if ok else 'no answer'})")
    return ok


def pause(appid: str) -> bool:
    return _call(f"SteamClient.Downloads.PauseAppUpdate({int(appid)}, cid)")


def resume(appid: str) -> bool:
    return _call(f"SteamClient.Downloads.ResumeAppUpdate({int(appid)}, cid)")


def pause_all(on: bool) -> bool:
    return _call(f"SteamClient.Downloads.EnableAllDownloads({'false' if on else 'true'}, cid)")


def move(appid: str, index: int) -> bool:
    return _call(f"SteamClient.Downloads.SetQueueIndex({int(appid)}, {int(index)}, cid)")
