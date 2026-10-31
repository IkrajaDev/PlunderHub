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

"""A game's windows on the desktop: on the workspace right of
PlunderHub's.

A game's window is seldom PlunderHub's child - Steam, Heroic or Lutris
start it - so a launch can't just be told where to open or how. Instead
WorkspaceMover watches the compositor's windows while the game runs, and
each new one that belongs to the game is moved to the next workspace
(the first one followed there). Hyprland,
Sway, i3 and niri over their IPC, KDE Plasma through KWin scripts, and
every other desktop - GNOME, Xfce, Cinnamon, MATE, any X11 window
manager - through the X11 desktop hints (EWMH); on Wayland those reach
the XWayland windows only, which is what Wine and Proton games are."""

import ctypes
import ctypes.util
import json
import os
import re
import struct
import subprocess
import shutil
import socket
import tempfile
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Set, Tuple

from plunderhub.core.utils import (
    GameProcessMatch, _is_under, _read_proc_cmdline, _read_proc_environ, _STEAM_APPID_ENV_KEYS,
    process_start_time, which,
)
from plunderhub.logging_setup import logger

# (window id, pid, workspace)
Window = Tuple[str, int, str]


def _run(cmd: List[str], timeout: float = 2.0) -> Optional[str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"{cmd[0]} failed: {e}")
        return None
    return done.stdout if done.returncode == 0 else None


def _run_json(cmd: List[str]):
    out = _run(cmd)
    if out is None:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


class Compositor:
    name = ""
    # Only XWayland windows can be moved - native Wayland ones stay put
    xwayland_only = False

    def last_focused(self):
        """The current workspace as the last windows() answer had it,
        where that answer holds it (KWin); else None."""
        return None

    def windows(self) -> Optional[List[Window]]:
        raise NotImplementedError

    def next_workspace(self, workspace: str) -> Optional[str]:
        """The one right of it - numbered workspaces only."""
        try:
            number = int(workspace)
        except ValueError:
            return None
        return str(number + 1) if number > 0 else None

    def focused_workspace(self) -> Optional[str]:
        raise NotImplementedError

    def move(self, window: str, workspace: str, follow: bool) -> bool:
        raise NotImplementedError

    def close_window(self, window: str) -> bool:
        """Asks the window to close, as its close button would."""
        return False

    def focus(self, window: Optional[str], workspace: Optional[str]) -> bool:
        """Brings window to the front, its workspace shown - or, without
        one, just shows workspace."""
        return False

    def corner_radius(self) -> Optional[int]:
        """The corner rounding the compositor gives every window it
        draws, PlunderHub's undecorated dialogs included - None when it
        doesn't round them (they keep their own)."""
        return None

    def workspace_of_pid(self, pid: int) -> Optional[str]:
        for _wid, wpid, ws in self.windows() or []:
            if wpid == pid:
                return ws
        return None


def _lua_str(value) -> str:
    """A Lua string literal: a workspace named in the user's own config
    may hold a quote or a backslash (JSON's escapes are Lua's too)."""
    return json.dumps(str(value), ensure_ascii=False)


class Hyprland(Compositor):
    name = "Hyprland"
    # None until a dispatch says which: the classic dispatchers or, with
    # a Lua config, hl.dsp calls
    _lua: Optional[bool] = None

    def windows(self):
        data = _run_json(["hyprctl", "clients", "-j"])
        if not isinstance(data, list):
            return None
        return [(c.get("address", ""), int(c.get("pid", 0)), self._ref(c.get("workspace") or {}))
                for c in data if c.get("mapped", True)]

    @staticmethod
    def _ref(workspace: dict) -> str:
        """How a dispatcher names it: its number, name:<name> for a named
        one (their ids are negative), special:<name> as it is."""
        wsid, name = int(workspace.get("id", 0) or 0), str(workspace.get("name", ""))
        if wsid > 0:
            return str(wsid)
        return name if name.startswith("special") else f"name:{name}" if name else ""

    def next_workspace(self, workspace):
        """A numbered one's next number; after a named one, the next of
        its monitor's workspaces in Hyprland's own order (m+1, by id),
        else the lowest free number."""
        found = super().next_workspace(workspace)
        if found is not None or not workspace:
            return found
        spaces = _run_json(["hyprctl", "workspaces", "-j"])
        if not isinstance(spaces, list):
            return None
        here = next((w for w in spaces if isinstance(w, dict) and self._ref(w) == workspace), None)
        if here is None:
            return None
        later = sorted((w for w in spaces if isinstance(w, dict) and w.get("monitor") == here.get("monitor")
                        and int(w.get("id", 0) or 0) > int(here.get("id", 0) or 0)
                        and not str(w.get("name", "")).startswith("special")),
                       key=lambda w: int(w.get("id", 0) or 0))
        if later:
            return self._ref(later[0])
        used = {int(w.get("id", 0) or 0) for w in spaces if isinstance(w, dict)}
        return str(next(n for n in range(1, len(used) + 2) if n not in used))

    def corner_radius(self):
        data = _run_json(["hyprctl", "getoption", "decoration:rounding", "-j"])
        value = data.get("int") if isinstance(data, dict) else None
        return int(value) if isinstance(value, int) and value >= 0 else None

    def focused_workspace(self):
        data = _run_json(["hyprctl", "activeworkspace", "-j"])
        # Hyprland 0.57 leaves the id out for a named workspace
        return (self._ref(data) or None) if isinstance(data, dict) and ("id" in data or data.get("name")) else None

    def move(self, window, workspace, follow):
        classic = (f"movetoworkspace {workspace},address:{window}" if follow
                   else f"movetoworkspacesilent {workspace},address:{window}")
        lua = (f"hl.dsp.window.move({{ workspace = {_lua_str(workspace)}, window = 'address:{window}', "
               f"follow = {'true' if follow else 'false'} }})")
        return self._dispatch(["dispatch", classic], ["dispatch", lua])

    def focus(self, window, workspace):
        if window:
            return self._dispatch(["dispatch", f"focuswindow address:{window}"],
                                  ["dispatch", f"hl.dsp.focus({{ window = 'address:{window}' }})"])
        return bool(workspace) and self._dispatch(["dispatch", f"workspace {workspace}"],
                                                  ["dispatch", f"hl.dsp.focus({{ workspace = {_lua_str(workspace)} }})"])

    def close_window(self, window):
        return self._dispatch(["dispatch", f"closewindow address:{window}"],
                              ["dispatch", f"hl.dsp.window.close({{ window = 'address:{window}' }})"])

    def _dispatch(self, classic: List[str], lua: List[str]) -> bool:
        for use_lua in ([self._lua] if self._lua is not None else [False, True]):
            out = _run(["hyprctl", *(lua if use_lua else classic)])
            if out is not None and out.strip() and all(part.strip() == "ok" for part in out.split("\n\n")
                                                       if part.strip()):
                Hyprland._lua = use_lua
                return True
        return False


class Sway(Compositor):
    """Sway, and i3 with the same IPC (pids from xprop there)."""

    def __init__(self, msg: str):
        self.msg = msg
        self.name = "Sway" if msg == "swaymsg" else "i3"
        self._x11_pids: Dict[int, Optional[int]] = {}

    def windows(self):
        tree = _run_json([self.msg, "-t", "get_tree"])
        if not isinstance(tree, dict):
            return None
        found: List[Window] = []

        def walk(node, workspace):
            if node.get("type") == "workspace":
                workspace = self._ref(node)
            pid = node.get("pid")
            if pid is None and node.get("window"):
                xid = node["window"]
                if xid not in self._x11_pids:
                    self._x11_pids[xid] = _x11_pid(xid)
                pid = self._x11_pids[xid]
            if pid and node.get("type") in ("con", "floating_con") and not node.get("nodes"):
                found.append((str(node.get("id")), int(pid), workspace or ""))
            for child in node.get("nodes", []) + node.get("floating_nodes", []):
                walk(child, workspace)
        walk(tree, None)
        return found

    @staticmethod
    def _ref(workspace: dict) -> str:
        """Its number, or its name for a named one (num is -1 then)."""
        num = workspace.get("num", -1)
        return str(num) if isinstance(num, int) and num >= 0 else str(workspace.get("name", ""))

    def next_workspace(self, workspace):
        """A numbered one's next number; after a named one, the next of
        its output's workspaces in Sway's own order, else the lowest
        free number."""
        found = super().next_workspace(workspace)
        if found is not None or not workspace:
            return found
        spaces = [w for w in _run_json([self.msg, "-t", "get_workspaces"]) or [] if isinstance(w, dict)]
        here = next((w for w in spaces if self._ref(w) == workspace), None)
        if here is None:
            return None
        mine = [w for w in spaces if w.get("output") == here.get("output")]
        later = mine[mine.index(here) + 1:]
        if later:
            return self._ref(later[0])
        used = {w.get("num") for w in spaces}
        return str(next(n for n in range(1, len(used) + 2) if n not in used))

    def focused_workspace(self):
        data = _run_json([self.msg, "-t", "get_workspaces"])
        for ws in data or []:
            if ws.get("focused"):
                return self._ref(ws)
        return None

    def move(self, window, workspace, follow):
        where = (f"number {workspace}" if workspace.isdigit()
                 else '"' + workspace.replace("\\", "\\\\").replace('"', '\\"') + '"')
        cmd = f"[con_id={window}] move container to workspace {where}"
        if follow:
            cmd += f"; workspace {where}"
        return _run([self.msg, cmd]) is not None

    def close_window(self, window):
        # kill closes it politely, the way its close button does
        return _run([self.msg, f"[con_id={window}] kill"]) is not None

    def focus(self, window, workspace):
        if window:
            return _run([self.msg, f"[con_id={window}] focus"]) is not None
        if not workspace:
            return False
        where = (f"number {workspace}" if workspace.isdigit()
                 else '"' + workspace.replace("\\", "\\\\").replace('"', '\\"') + '"')
        return _run([self.msg, f"workspace {where}"]) is not None

def _x11_pid(xid: int) -> Optional[int]:
    out = _run(["xprop", "-id", str(xid), "_NET_WM_PID"]) if which("xprop") else None
    match = re.search(r"= (\d+)", out or "")
    return int(match.group(1)) if match else None


class Niri(Compositor):
    """niri's workspaces are numbered per monitor, top to bottom - the
    next one is the one below, on the same monitor. Two monitors both have
    an index 2, and an index given to niri means the focused monitor's -
    so workspaces go by their unique id here (niri-ipc Workspace.id), and
    moves go over niri's IPC socket, which takes an id (the CLI doesn't)."""
    name = "niri"

    def _workspaces(self) -> Dict[int, dict]:
        data = _run_json(["niri", "msg", "--json", "workspaces"])
        return {ws["id"]: ws for ws in data or [] if isinstance(ws, dict) and "id" in ws}

    def windows(self):
        data = _run_json(["niri", "msg", "--json", "windows"])
        if not isinstance(data, list):
            return None
        return [(str(w.get("id")), int(w.get("pid") or 0),
                 str(w["workspace_id"]) if w.get("workspace_id") is not None else "") for w in data]

    def focused_workspace(self):
        for ws in self._workspaces().values():
            if ws.get("is_focused"):
                return str(ws["id"])
        return None

    def next_workspace(self, workspace: str) -> Optional[str]:
        spaces = self._workspaces()
        try:
            here = spaces.get(int(workspace))
        except ValueError:
            return None
        if here is None:
            return None
        below = [ws for ws in spaces.values()
                 if ws.get("output") == here.get("output") and ws.get("idx") == (here.get("idx") or 0) + 1]
        return str(below[0]["id"]) if below else None

    def _ipc(self, action: dict) -> bool:
        """One action over $NIRI_SOCKET (JSON, a line each way)."""
        path = os.environ.get("NIRI_SOCKET")
        if not path:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2.0)
                sock.connect(path)
                sock.sendall(json.dumps({"Action": action}).encode() + b"\n")
                reply = sock.makefile("rb").readline()
            return "Ok" in json.loads(reply.decode() or "{}")
        except (OSError, ValueError) as e:
            logger.debug(f"niri IPC failed: {e}")
            return False

    def _index_on_focused(self, workspace: str) -> Optional[str]:
        """Without the socket: the index, right only on the focused monitor."""
        try:
            ws = self._workspaces().get(int(workspace))
        except ValueError:
            return None
        return str(ws.get("idx")) if ws else None

    def move(self, window, workspace, follow):
        try:
            if self._ipc({"MoveWindowToWorkspace": {"window_id": int(window), "reference": {"Id": int(workspace)},
                                                    "focus": bool(follow)}}):
                return True
        except ValueError:
            return False
        index = self._index_on_focused(workspace)
        return index is not None and _run(["niri", "msg", "action", "move-window-to-workspace", "--window-id",
                                           window, "--focus", "true" if follow else "false", index]) is not None

    def close_window(self, window):
        return _run(["niri", "msg", "action", "close-window", "--id", window]) is not None

    def focus(self, window, workspace):
        if window:
            return _run(["niri", "msg", "action", "focus-window", "--id", window]) is not None
        if not workspace:
            return False
        try:
            if self._ipc({"FocusWorkspace": {"reference": {"Id": int(workspace)}}}):
                return True
        except ValueError:
            return False
        index = self._index_on_focused(workspace)
        return index is not None and _run(["niri", "msg", "action", "focus-workspace", index]) is not None


# Run in KWin by KWin's scripting D-Bus API, the reply sent back over
# D-Bus: Plasma 6 (windowList, desktop objects) and Plasma 5 (clientList,
# desktop numbers). Workspaces are their 1-based position.
_KWIN_SCRIPT = """(function () {
    var six = typeof workspace.windowList === "function";
    var wins = six ? workspace.windowList() : workspace.clientList();
    function count() { return six ? workspace.desktops.length : workspace.desktops; }
    function position(desktop) {
        for (var i = 0; i < workspace.desktops.length; i++)
            if (workspace.desktops[i].id === desktop.id) return i + 1;
        return 0;
    }
    function current() { return six ? position(workspace.currentDesktop) : workspace.currentDesktop; }
    function desktopOf(w) {
        if (!six) return w.desktop > 0 ? w.desktop : 0;
        return w.desktops.length === 1 ? position(w.desktops[0]) : 0;
    }
    function idOf(w) { return String(w.internalId); }
    function find(id) {
        for (var i = 0; i < wins.length; i++)
            if (idOf(wins[i]) === id) return wins[i];
        return null;
    }
    function ensure(n) {
        for (var tries = 0; count() < n && tries < 20; tries++) {
            if (six) workspace.createDesktop(count(), "");
            else workspace.desktops = n;
        }
        return count() >= n;
    }
    var result = (function () { %BODY% })();
    callDBus(%SERVICE%, %PATH%, %IFACE%, "Reply", %TOKEN%, JSON.stringify(result === undefined ? null : result));
})();
"""

_KWIN_LIST = """
    var found = [];
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        if (w.normalWindow) found.push([idOf(w), w.pid, desktopOf(w)]);
    }
    return {current: current(), windows: found};
"""

_KWIN_MOVE = """
    var w = find(%WINDOW%), n = %WORKSPACE%;
    if (!w || !ensure(n)) return false;
    if (six) w.desktops = [workspace.desktops[n - 1]];
    else w.desktop = n;
    if (%FOLLOW%) {
        workspace.currentDesktop = six ? workspace.desktops[n - 1] : n;
        if (six) workspace.activeWindow = w;
        else workspace.activeClient = w;
    }
    return true;
"""

_KWIN_CLOSE = """
    var w = find(%WINDOW%);
    if (!w) return false;
    w.closeWindow();
    return true;
"""

_KWIN_FOCUS = """
    var w = find(%WINDOW%), n = %WORKSPACE%;
    if (w) {
        var d = desktopOf(w);
        if (d > 0) workspace.currentDesktop = six ? workspace.desktops[d - 1] : d;
        if (six) workspace.activeWindow = w;
        else workspace.activeClient = w;
        return true;
    }
    if (n < 1 || n > count()) return false;
    workspace.currentDesktop = six ? workspace.desktops[n - 1] : n;
    return true;
"""

_KWIN_BRIDGE_XML = """<node>
  <interface name="io.github.PlunderHub.KWin">
    <method name="Reply">
      <arg type="s" name="token" direction="in"/>
      <arg type="s" name="result" direction="in"/>
    </method>
  </interface>
</node>"""


class KWin(Compositor):
    """KDE Plasma, Wayland or X11: a short script run in KWin for each
    look or change, which calls PlunderHub back over D-Bus."""
    name = "KDE Plasma"
    _PATH = "/io/github/PlunderHub/KWin"
    _IFACE = "io.github.PlunderHub.KWin"

    def __init__(self, bus):
        self._bus = bus
        # one script at a time
        self._lock = threading.Lock()
        self._replies: Dict[str, str] = {}
        self._replied = threading.Condition()
        self._current: Optional[str] = None
        self._waiting: Optional[str] = None      # the token of the script being waited for
        self._registration = 0
        self._loop = None
        ready = threading.Event()
        threading.Thread(target=self._serve, args=(ready,), name="plunderhub-kwin", daemon=True).start()
        ready.wait(2.0)

    @classmethod
    def detect(cls) -> Optional["KWin"]:
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")
        if "KDE" not in desktop and not os.environ.get("KDE_FULL_SESSION"):
            return None
        try:
            from gi.repository import Gio, GLib
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            owned = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "NameHasOwner", GLib.Variant("(s)", ("org.kde.KWin",)),
                                  None, Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        except Exception as e:
            logger.debug(f"KWin D-Bus unreachable: {e}")
            return None
        if not owned:
            return None
        kwin = cls(bus)
        if kwin.windows() is None:
            logger.info("KWin scripting gave no answer - moving windows through X11 instead")
            kwin.close()
            return None
        return kwin

    def close(self):
        if self._registration:
            self._bus.unregister_object(self._registration)
            self._registration = 0
        if self._loop is not None:
            self._loop.quit()

    def _serve(self, ready: threading.Event):
        """Takes the scripts' replies on a main context of its own, so a
        look from the GTK thread doesn't wait on itself."""
        from gi.repository import Gio, GLib
        context = GLib.MainContext.new()
        context.push_thread_default()
        try:
            info = Gio.DBusNodeInfo.new_for_xml(_KWIN_BRIDGE_XML).interfaces[0]
            self._registration = self._bus.register_object(self._PATH, info, self._on_call, None, None)
        except Exception as e:
            logger.debug(f"Couldn't take KWin's replies: {e}")
            ready.set()
            return
        self._loop = GLib.MainLoop.new(context, False)
        ready.set()
        self._loop.run()

    def _on_call(self, _bus, _sender, _path, _iface, method, params, invocation):
        if method == "Reply":
            token, result = params.unpack()
            with self._replied:
                if token == self._waiting:      # a reply after its 2 s wait isn't kept
                    self._replies[token] = result
                    self._replied.notify_all()
        invocation.return_value(None)

    def _kwin(self, method: str, params=None):
        from gi.repository import Gio
        return self._bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", method, params,
                                   None, Gio.DBusCallFlags.NONE, 2000, None)

    def _script(self, body: str, **values):
        """What the script returns, or None when KWin doesn't answer."""
        from gi.repository import GLib
        token = uuid.uuid4().hex
        for key, value in values.items():
            body = body.replace(f"%{key.upper()}%", json.dumps(value))
        source = _KWIN_SCRIPT.replace("%BODY%", body)
        for key, value in (("SERVICE", self._bus.get_unique_name()), ("PATH", self._PATH),
                           ("IFACE", self._IFACE), ("TOKEN", token)):
            source = source.replace(f"%{key}%", json.dumps(value))
        plugin = f"plunderhub-{token}"
        with self._lock:
            # a folder per script, gone with it: none left in /tmp at exit
            folder = tempfile.mkdtemp(prefix="plunderhub-kwin-")
            path = os.path.join(folder, f"{token}.js")
            with self._replied:
                self._waiting = token
            try:
                with open(path, "w") as f:
                    f.write(source)
                if self._kwin("loadScript", GLib.Variant("(ss)", (path, plugin))).unpack()[0] < 0:
                    return None
                self._kwin("start")
                with self._replied:
                    self._replied.wait_for(lambda: token in self._replies, timeout=2.0)
                    result = self._replies.pop(token, None)
            except Exception as e:
                logger.debug(f"KWin script failed: {e}")
                return None
            finally:
                try:
                    self._kwin("unloadScript", GLib.Variant("(s)", (plugin,)))
                except Exception:
                    pass
                with self._replied:
                    self._waiting = None
                    self._replies.clear()
                shutil.rmtree(folder, ignore_errors=True)
        try:
            return json.loads(result) if result is not None else None
        except ValueError:
            return None

    def windows(self):
        data = self._script(_KWIN_LIST)
        if not isinstance(data, dict):
            return None
        found = [w for w in data.get("windows") or [] if isinstance(w, list) and len(w) >= 3]
        self._current = str(data.get("current") or "") or None
        return [(str(w[0]), int(w[1] or 0), str(w[2] or "")) for w in found]

    def focused_workspace(self):
        return self._current if self.windows() is not None else None

    def last_focused(self):
        return self._current        # from the windows() just asked

    def move(self, window, workspace, follow):
        return self._script(_KWIN_MOVE, window=window, workspace=int(workspace), follow=follow) is True

    def close_window(self, window):
        return self._script(_KWIN_CLOSE, window=window) is True

    def focus(self, window, workspace):
        try:
            n = int(workspace or 0)
        except ValueError:
            n = 0
        return self._script(_KWIN_FOCUS, window=window or "", workspace=n) is True

class _Cookie(ctypes.Structure):
    _fields_ = [("sequence", ctypes.c_uint)]


class _AtomReply(ctypes.Structure):
    _fields_ = [("response_type", ctypes.c_uint8), ("pad0", ctypes.c_uint8), ("sequence", ctypes.c_uint16),
                ("length", ctypes.c_uint32), ("atom", ctypes.c_uint32)]


class _PropertyReply(ctypes.Structure):
    _fields_ = [("response_type", ctypes.c_uint8), ("format", ctypes.c_uint8), ("sequence", ctypes.c_uint16),
                ("length", ctypes.c_uint32), ("type", ctypes.c_uint32), ("bytes_after", ctypes.c_uint32),
                ("value_len", ctypes.c_uint32), ("pad0", ctypes.c_uint8 * 12)]


class _ScreenIterator(ctypes.Structure):
    # data points at an xcb_screen_t, whose first field is its root
    _fields_ = [("data", ctypes.POINTER(ctypes.c_uint32)), ("rem", ctypes.c_int), ("index", ctypes.c_int)]


class _ClientIdSpec(ctypes.Structure):
    _fields_ = [("client", ctypes.c_uint32), ("mask", ctypes.c_uint32)]


class _ClientIdValue(ctypes.Structure):
    _fields_ = [("spec", _ClientIdSpec), ("length", ctypes.c_uint32)]


class _ClientIdIterator(ctypes.Structure):
    _fields_ = [("data", ctypes.POINTER(_ClientIdValue)), ("rem", ctypes.c_int), ("index", ctypes.c_int)]


def _load(name: str, soname: str):
    try:
        return ctypes.CDLL(ctypes.util.find_library(name) or soname)
    except OSError:
        return None


class _Xcb:
    """The few X11 calls the desktop hints need, through libxcb - which
    reports errors instead of ending the process the way Xlib does."""

    _CLIENT_PID = 2     # XCB_RES_CLIENT_ID_MASK_LOCAL_CLIENT_PID

    def __init__(self):
        lib = _load("xcb", "libxcb.so.1")
        if lib is None:
            raise OSError("no libxcb")
        self.lib = lib
        self.libc = ctypes.CDLL(None)
        self.libc.free.argtypes = [ctypes.c_void_p]
        lib.xcb_connect.restype = ctypes.c_void_p
        lib.xcb_connect.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        lib.xcb_connection_has_error.argtypes = [ctypes.c_void_p]
        lib.xcb_disconnect.argtypes = [ctypes.c_void_p]
        lib.xcb_get_setup.restype = ctypes.c_void_p
        lib.xcb_get_setup.argtypes = [ctypes.c_void_p]
        lib.xcb_setup_roots_iterator.restype = _ScreenIterator
        lib.xcb_setup_roots_iterator.argtypes = [ctypes.c_void_p]
        lib.xcb_screen_next.argtypes = [ctypes.POINTER(_ScreenIterator)]
        lib.xcb_intern_atom.restype = _Cookie
        lib.xcb_intern_atom.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_char_p]
        lib.xcb_intern_atom_reply.restype = ctypes.POINTER(_AtomReply)
        lib.xcb_intern_atom_reply.argtypes = [ctypes.c_void_p, _Cookie, ctypes.POINTER(ctypes.c_void_p)]
        lib.xcb_get_property.restype = _Cookie
        lib.xcb_get_property.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint32,
                                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        lib.xcb_get_property_reply.restype = ctypes.POINTER(_PropertyReply)
        lib.xcb_get_property_reply.argtypes = [ctypes.c_void_p, _Cookie, ctypes.POINTER(ctypes.c_void_p)]
        lib.xcb_get_property_value.restype = ctypes.c_void_p
        lib.xcb_get_property_value.argtypes = [ctypes.POINTER(_PropertyReply)]
        lib.xcb_get_property_value_length.argtypes = [ctypes.POINTER(_PropertyReply)]
        lib.xcb_send_event.restype = _Cookie
        lib.xcb_send_event.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_char_p]
        lib.xcb_flush.argtypes = [ctypes.c_void_p]
        screen = ctypes.c_int(0)
        self.conn = lib.xcb_connect(None, ctypes.byref(screen))
        if not self.conn or lib.xcb_connection_has_error(self.conn):
            self.close()        # an error connection must be freed all the same
            raise OSError("no X display")
        roots = lib.xcb_setup_roots_iterator(lib.xcb_get_setup(self.conn))
        for _ in range(max(0, min(screen.value, roots.rem - 1))):
            lib.xcb_screen_next(ctypes.byref(roots))
        self.root = roots.data[0]
        self._atoms: Dict[str, int] = {}
        self.res = _load("xcb-res", "libxcb-res.so.0")
        if self.res is not None:
            self.res.xcb_res_query_client_ids.restype = _Cookie
            self.res.xcb_res_query_client_ids.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                          ctypes.POINTER(_ClientIdSpec)]
            self.res.xcb_res_query_client_ids_reply.restype = ctypes.c_void_p
            self.res.xcb_res_query_client_ids_reply.argtypes = [ctypes.c_void_p, _Cookie,
                                                                ctypes.POINTER(ctypes.c_void_p)]
            self.res.xcb_res_query_client_ids_ids_iterator.restype = _ClientIdIterator
            self.res.xcb_res_query_client_ids_ids_iterator.argtypes = [ctypes.c_void_p]
            self.res.xcb_res_client_id_value_next.argtypes = [ctypes.POINTER(_ClientIdIterator)]
            self.res.xcb_res_client_id_value_value.restype = ctypes.POINTER(ctypes.c_uint32)
            self.res.xcb_res_client_id_value_value.argtypes = [ctypes.POINTER(_ClientIdValue)]


    def close(self):
        if self.conn:
            self.lib.xcb_disconnect(self.conn)
            self.conn = None
    def _free_error(self, error: ctypes.c_void_p):
        if error.value:
            self.libc.free(error)

    def atom(self, name: str) -> int:
        if name not in self._atoms:
            raw = name.encode()
            error = ctypes.c_void_p()
            reply = self.lib.xcb_intern_atom_reply(
                self.conn, self.lib.xcb_intern_atom(self.conn, 0, len(raw), raw), ctypes.byref(error))
            self._free_error(error)
            if not reply:
                return 0
            self._atoms[name] = reply.contents.atom
            self.libc.free(reply)
        return self._atoms[name]

    def cardinals(self, window: int, name: str) -> Optional[List[int]]:
        """A 32-bit property (CARDINAL, WINDOW, ATOM), None when unset."""
        error = ctypes.c_void_p()
        reply = self.lib.xcb_get_property_reply(
            self.conn, self.lib.xcb_get_property(self.conn, 0, window, self.atom(name), 0, 0, 4096),
            ctypes.byref(error))
        self._free_error(error)
        if not reply:
            return None
        try:
            if reply.contents.format != 32 or reply.contents.type == 0:
                return None
            size = self.lib.xcb_get_property_value_length(reply)
            data = ctypes.string_at(self.lib.xcb_get_property_value(reply), size)
            return list(struct.unpack(f"={size // 4}I", data[:size // 4 * 4]))
        finally:
            self.libc.free(reply)

    def text(self, window: int, name: str) -> str:
        error = ctypes.c_void_p()
        reply = self.lib.xcb_get_property_reply(
            self.conn, self.lib.xcb_get_property(self.conn, 0, window, self.atom(name), 0, 0, 1024),
            ctypes.byref(error))
        self._free_error(error)
        if not reply:
            return ""
        try:
            size = self.lib.xcb_get_property_value_length(reply)
            return ctypes.string_at(self.lib.xcb_get_property_value(reply), size).decode("utf-8", "replace")
        finally:
            self.libc.free(reply)

    def client_pid(self, window: int) -> Optional[int]:
        """The pid as the X server sees its client - the host's, where
        _NET_WM_PID holds a Flatpak sandbox's own."""
        if self.res is None:
            return None
        spec = _ClientIdSpec(window, self._CLIENT_PID)
        error = ctypes.c_void_p()
        reply = self.res.xcb_res_query_client_ids_reply(
            self.conn, self.res.xcb_res_query_client_ids(self.conn, 1, ctypes.byref(spec)), ctypes.byref(error))
        self._free_error(error)
        if not reply:
            return None
        try:
            ids = self.res.xcb_res_query_client_ids_ids_iterator(reply)
            while ids.rem > 0:
                value = ids.data.contents
                if value.spec.mask & self._CLIENT_PID and value.length >= 4:
                    return int(self.res.xcb_res_client_id_value_value(ids.data)[0])
                self.res.xcb_res_client_id_value_next(ctypes.byref(ids))
            return None
        finally:
            self.libc.free(ctypes.c_void_p(reply))

    def message(self, window: int, name: str, *data: int):
        """A client message to the window manager, as pagers send them."""
        values = (list(data) + [0] * 5)[:5]
        event = struct.pack("=BBHII5I", 33, 32, 0, window, self.atom(name), *values)
        # SubstructureNotify | SubstructureRedirect
        self.lib.xcb_send_event(self.conn, 0, self.root, (1 << 19) | (1 << 20), event)
        self.lib.xcb_flush(self.conn)


class Ewmh(Compositor):
    """Any window manager that keeps the X11 desktop hints: GNOME, Xfce,
    Cinnamon, MATE, LXQt, Openbox... On a Wayland session they cover
    the XWayland windows - Wine and Proton games, most native ones -
    while PlunderHub's own window counts as on the focused workspace.
    Workspaces are their 1-based position."""

    _STICKY = 0xFFFFFFFF
    _NEEDED = ("_NET_WM_DESKTOP", "_NET_CURRENT_DESKTOP", "_NET_NUMBER_OF_DESKTOPS", "_NET_CLIENT_LIST")

    def __init__(self, xcb: _Xcb, name: str):
        self._x = xcb
        self.name = name
        self.xwayland_only = bool(os.environ.get("WAYLAND_DISPLAY"))
        self._lock = threading.Lock()

    @classmethod
    def detect(cls) -> Optional["Ewmh"]:
        if not os.environ.get("DISPLAY"):
            return None
        try:
            xcb = _Xcb()
        except (OSError, AttributeError) as e:
            logger.debug(f"No X11 desktop hints: {e}")
            return None
        supported = set(xcb.cardinals(xcb.root, "_NET_SUPPORTED") or [])
        check = xcb.cardinals(xcb.root, "_NET_SUPPORTING_WM_CHECK")
        if not check or not all(xcb.atom(a) in supported for a in cls._NEEDED):
            xcb.close()         # tried again at later launches: no connection per try
            return None
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")[0]
        if desktop.startswith("X-"):
            desktop = desktop[2:]
        name = desktop or xcb.text(check[0], "_NET_WM_NAME") or "X11"
        return cls(xcb, name)

    def windows(self):
        with self._lock:
            clients = self._x.cardinals(self._x.root, "_NET_CLIENT_LIST")
            if clients is None:
                return None
            found: List[Window] = []
            for wid in clients:
                desktop = (self._x.cardinals(wid, "_NET_WM_DESKTOP") or [self._STICKY])[0]
                pid = self._x.client_pid(wid) or (self._x.cardinals(wid, "_NET_WM_PID") or [0])[0]
                found.append((str(wid), int(pid), "" if desktop == self._STICKY else str(desktop + 1)))
        return found

    def focused_workspace(self):
        with self._lock:
            current = self._x.cardinals(self._x.root, "_NET_CURRENT_DESKTOP")
            if not current and self._x.cardinals(self._x.root, "_NET_NUMBER_OF_DESKTOPS"):
                # Mutter sets it at the first switch - until then, the first
                current = [0]
        return str(current[0] + 1) if current else None

    def move(self, window, workspace, follow):
        index = int(workspace) - 1
        with self._lock:
            count = (self._x.cardinals(self._x.root, "_NET_NUMBER_OF_DESKTOPS") or [0])[0]
            if index >= count:
                # GNOME keeps an empty one at the end itself; others are asked
                self._x.message(self._x.root, "_NET_NUMBER_OF_DESKTOPS", index + 1)
                for _ in range(20):
                    time.sleep(0.05)
                    count = (self._x.cardinals(self._x.root, "_NET_NUMBER_OF_DESKTOPS") or [0])[0]
                    if index < count:
                        break
                else:
                    return False
            # source 2: a pager, which window managers obey
            self._x.message(int(window), "_NET_WM_DESKTOP", index, 2)
            if follow:
                self._x.message(self._x.root, "_NET_CURRENT_DESKTOP", index, 0)
                self._x.message(int(window), "_NET_ACTIVE_WINDOW", 2, 0, 0)
        return True

    def close_window(self, window):
        with self._lock:
            # source 2: a pager
            self._x.message(int(window), "_NET_CLOSE_WINDOW", 0, 2)
        return True

    def focus(self, window, workspace):
        """On a Wayland session PlunderHub's own window isn't an X11 one:
        then only its workspace is shown (GTK raises the window)."""
        with self._lock:
            if window:
                desktop = (self._x.cardinals(int(window), "_NET_WM_DESKTOP") or [self._STICKY])[0]
                if desktop != self._STICKY:
                    self._x.message(self._x.root, "_NET_CURRENT_DESKTOP", desktop, 0)
                self._x.message(int(window), "_NET_ACTIVE_WINDOW", 2, 0, 0)
                return True
            if workspace and workspace.isdigit() and int(workspace) > 0:
                self._x.message(self._x.root, "_NET_CURRENT_DESKTOP", int(workspace) - 1, 0)
                return True
        return False


def detect_compositor() -> Optional[Compositor]:
    """The desktop PlunderHub runs on, if it can move windows on it."""
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and which("hyprctl"):
        return Hyprland()
    if os.environ.get("SWAYSOCK") and which("swaymsg"):
        return Sway("swaymsg")
    if os.environ.get("NIRI_SOCKET") and which("niri"):
        return Niri()
    if os.environ.get("I3SOCK") and which("i3-msg"):
        return Sway("i3-msg")
    found = KWin.detect() or Ewmh.detect()
    if found is None:
        logger.info("No way to move game windows on this desktop - games open where their launcher puts them")
    return found


_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:\\")


def _wine_path(arg: str) -> str:
    """Z:\\home\\x\\game.exe -> /home/x/game.exe (Wine's Z: is /)."""
    if _WINDOWS_DRIVE.match(arg) and arg[0].lower() == "z":
        return arg[2:].replace("\\", "/")
    return arg


def _started_at(pid: int) -> Optional[float]:
    """When the process started, as time.time()."""
    ticks = process_start_time(pid)
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        return time.time() - uptime + ticks / os.sysconf("SC_CLK_TCK") if ticks is not None else None
    except (OSError, ValueError, IndexError):
        return None


def window_belongs_to_game(pid: int, match: GameProcessMatch, launched_at: float) -> bool:
    """Whether the process behind a new window is the game's: started
    since the launch, and Steam's AppId on it, or its program, a file it
    was given, or its working directory in the game's folder, or its
    Wine prefix the game's."""
    started = _started_at(pid)
    if started is not None and started < launched_at - 2:
        return False
    env = _read_proc_environ(pid)
    if match.steam_appid and any(env.get(k) == match.steam_appid for k in _STEAM_APPID_ENV_KEYS):
        return True
    if match.wineprefix and env.get("WINEPREFIX"):
        try:
            if os.path.realpath(env["WINEPREFIX"]) == os.path.realpath(match.wineprefix):
                return True
        except OSError:
            pass
    root = match.directory
    if not root or not os.path.isdir(root):
        return False
    root = os.path.realpath(root)
    paths = []
    for link in ("exe", "cwd"):
        try:
            paths.append(os.readlink(f"/proc/{pid}/{link}"))
        except OSError:
            pass
    paths += [_wine_path(a) for a in _read_proc_cmdline(pid) if a.startswith(("/", "Z:", "z:"))]
    return any(_is_under(p, root) for p in paths)


_STEAM_CLIENT_NAMES = ("steam", "steamwebhelper")


def is_steam_client(pid: int) -> bool:
    """Whether the process is Steam's own (its client or the web helper
    that draws its windows) - not a game Steam started."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            comm = f.read().strip()
    except OSError:
        return False
    if comm not in _STEAM_CLIENT_NAMES:
        return False
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        # someone else's process, or the sandbox hides it: the name says
        return True
    return "/ubuntu12_" in exe or os.path.basename(exe) in _STEAM_CLIENT_NAMES


class WorkspaceMover:
    """Moves a launched game's windows next to PlunderHub's workspace."""

    def __init__(self, compositor: Compositor, interval: float = 0.7):
        self.compositor = compositor
        self._interval = interval
        self._lock = threading.Lock()
        # uid -> (match, target workspace, launched at, windows already seen)
        self._games: Dict[str, Tuple[GameProcessMatch, str, float, Set[str]]] = {}
        self._followed: Set[str] = set()
        # Steam launches whose Steam stays in the tray: uid -> (whether a
        # new Steam window is unwanted now - None once it's over -,
        # windows already seen)
        self._steam_quiet: Dict[str, Tuple[Callable[[], Optional[bool]], Set[str]]] = {}
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        # PlunderHub's workspace at the last launch, and per game watched
        self._last_home: Optional[str] = None
        self._homes: Dict[str, str] = {}
        # the controller's switch: per game, its window and workspace when
        # PlunderHub came over it
        self._switched_from: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

    # how far right an empty workspace is looked for
    _EMPTY_SEARCH = 32

    def target_for_launch(self) -> Optional[str]:
        """The workspace right of PlunderHub's (or of the focused one) when
        nothing is open on it - else the next one right of that with
        nothing on it, so a game never lands among other windows."""
        # One look at the windows for the whole launch (watch and
        # keep_steam_in_tray take it too): on KWin each look is a script
        # round trip with a 2 s timeout, and this runs on the GTK thread.
        windows = self.compositor.windows()
        self._launch_snapshot = (time.monotonic(), windows)
        windows = windows or []
        mine = os.getpid()
        here = next((ws for _wid, pid, ws in windows if pid == mine and ws), None)
        if here is None:
            # KWin's answer already held the current desktop
            here = self.compositor.last_focused() if windows else None
            here = here or self.compositor.focused_workspace()
        self._last_home = here
        busy = {ws for _wid, pid, ws in windows if ws and pid != mine}
        target = self.compositor.next_workspace(here) if here else None
        first = target
        for _ in range(self._EMPTY_SEARCH):
            if target is None or target not in busy:
                break
            target = self.compositor.next_workspace(target)
        if target is None:
            logger.info(f"No workspace next to PlunderHub's ({here or 'unknown'}) on "
                        f"{self.compositor.name} - the game opens where it opens")
        elif target != first:
            logger.info(f"Workspace {first} has windows on it - the game goes to {target}")
        return target

    def _windows_now(self):
        """The launch's own look at the windows when it's a moment old,
        else a new one."""
        snap = getattr(self, "_launch_snapshot", None)
        if snap is not None and time.monotonic() - snap[0] < 3.0 and snap[1] is not None:
            return snap[1]
        return self.compositor.windows()

    def watch(self, uid: str, match: GameProcessMatch, target: str):
        """target: the workspace to move the game's windows to."""
        seen = {wid for wid, _pid, _ws in self._windows_now() or []}
        with self._lock:
            self._games[uid] = (match, target, time.time(), seen)
            if self._last_home:
                self._homes[uid] = self._last_home
            self._followed.discard(uid)
            self._start_locked()
        self._wake.set()

    def keep_steam_in_tray(self, uid: str, unwanted: Callable[[], Optional[bool]]):
        """Closes Steam windows that come up during the launch - Steam
        was started -silent, but opens its main window by itself at some
        steps (processing shaders); closed, it goes back to the tray.
        Windows open before the launch stay, and so does any while
        unwanted() is False (Steam asking the player something); it's
        followed until unwanted() gives None."""
        seen = {wid for wid, _pid, _ws in self._windows_now() or []}
        with self._lock:
            self._steam_quiet[uid] = (unwanted, seen)
            self._start_locked()
        self._wake.set()

    def _start_locked(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="plunderhub-workspaces", daemon=True)
            self._thread.start()

    def _quiet_steam(self, windows: List[Window]):
        with self._lock:
            quiet = dict(self._steam_quiet)
        for uid, (unwanted, seen) in quiet.items():
            try:
                now = unwanted()
            except Exception:
                now = None
            if now is None:
                with self._lock:
                    self._steam_quiet.pop(uid, None)
                continue
            for wid, pid, _ws in windows:
                if wid in seen:
                    continue
                seen.add(wid)
                if now and pid and is_steam_client(pid):
                    ok = self.compositor.close_window(wid)
                    logger.info(f"Steam opened its window during {uid}'s launch - back to the tray on "
                                f"{self.compositor.name}: {'done' if ok else 'failed'}")

    def forget(self, uid: str):
        with self._lock:
            self._games.pop(uid, None)
            self._followed.discard(uid)
            self._homes.pop(uid, None)
            self._switched_from.pop(uid, None)

    def back_home(self, uid: str):
        """The game moved next door has quit: back to PlunderHub's
        workspace, its window in front - when the player is still on the
        game's workspace (not if they went elsewhere meanwhile) and no
        other game it moved is running. Then forgets the game."""
        with self._lock:
            entry = self._games.get(uid)
            home = self._homes.get(uid)
            others = [u for u in self._games if u != uid]
        self.forget(uid)
        if entry is None or others:
            return
        target = entry[1]

        def work():
            focused = self.compositor.focused_workspace()
            if focused is not None and focused != target:
                logger.info(f"Game quit - not back to PlunderHub: workspace {focused} is in front, "
                            f"not the game's {target}")
                return
            mine = [(wid, ws) for wid, pid, ws in self.compositor.windows() or [] if pid == os.getpid()]
            window = next((wid for wid, ws in mine if ws == home), mine[0][0] if mine else None)
            ok = self.compositor.focus(window, home)
            logger.info(f"Game quit - back to PlunderHub's workspace {home or '?'} on "
                        f"{self.compositor.name}: {'done' if ok else 'failed'}")
        threading.Thread(target=work, name="plunderhub-workspace-home", daemon=True).start()

    def _game_window(self, uid: str, match: Optional[GameProcessMatch], launched_at: float,
                     windows: List[Window]) -> Tuple[Optional[str], Optional[str]]:
        """The game's window and its workspace: the one it was on when
        PlunderHub came over it, else one of its processes', else just
        the workspace it was moved to (or None, None)."""
        mine = os.getpid()
        last = self._switched_from.get(uid)
        if last is not None and any(wid == last[0] for wid, _pid, _ws in windows):
            ws = next(ws for wid, _pid, ws in windows if wid == last[0])
            return last[0], ws or last[1]
        if match is not None:
            for wid, pid, ws in windows:
                if pid and pid != mine and window_belongs_to_game(pid, match, launched_at):
                    return wid, ws
        with self._lock:
            entry = self._games.get(uid)
        if entry is not None:
            return None, entry[1]
        return (None, last[1]) if last is not None else (None, None)

    def to_plunderhub(self, uid: str, match: Optional[GameProcessMatch], launched_at: float):
        """The controller's switch, the game in front: PlunderHub's window
        to the front, its workspace shown - the game's window remembered
        for the way back. Tries for a second while a window just shown
        (from the tray) isn't on the desktop's list yet."""
        def work():
            mine = os.getpid()
            window = home = None
            for _ in range(10):
                windows = self.compositor.windows() or []
                game = self._game_window(uid, match, launched_at, windows)
                if game != (None, None):
                    self._switched_from[uid] = game
                ours = [(wid, ws) for wid, pid, ws in windows if pid == mine]
                if ours:
                    home = self._homes.get(uid)
                    window, home = next(((w, ws) for w, ws in ours if ws == home), ours[0])
                    break
                time.sleep(0.1)
            ok = self.compositor.focus(window, home)
            logger.info(f"Controller switch - to PlunderHub (workspace {home or '?'}) on "
                        f"{self.compositor.name}: {'done' if ok else 'failed'}")
        threading.Thread(target=work, name="plunderhub-switch", daemon=True).start()

    def to_game(self, uid: str, match: Optional[GameProcessMatch], launched_at: float,
                done: Optional[Callable[[bool], None]] = None):
        """The controller's switch, PlunderHub in front: back to the game's
        window (or at least its workspace). done(ok) runs in the worker."""
        def work():
            window, ws = self._game_window(uid, match, launched_at, self.compositor.windows() or [])
            ok = (window is not None or ws is not None) and self.compositor.focus(window, ws)
            logger.info(f"Controller switch - to {uid} ({'window' if window else 'workspace'} "
                        f"{window or ws or '?'}) on {self.compositor.name}: {'done' if ok else 'failed'}")
            if done is not None:
                done(bool(ok))
        threading.Thread(target=work, name="plunderhub-switch", daemon=True).start()

    def _run(self):
        while True:
            with self._lock:
                games = dict(self._games)
                if not games and not self._steam_quiet:
                    self._thread = None
                    return
            windows = self.compositor.windows() or []
            self._quiet_steam(windows)
            for uid, (match, target, launched_at, seen) in games.items():
                for wid, pid, ws in windows:
                    if wid in seen:
                        continue
                    seen.add(wid)
                    if not pid or not window_belongs_to_game(pid, match, launched_at):
                        continue
                    if ws != target:
                        with self._lock:
                            follow = uid not in self._followed
                            self._followed.add(uid)
                        ok = self.compositor.move(wid, target, follow)
                        logger.info(f"Moving {uid}'s window to workspace {target} on "
                                    f"{self.compositor.name}: {'done' if ok else 'failed'}")
            self._wake.wait(self._interval)
            self._wake.clear()
