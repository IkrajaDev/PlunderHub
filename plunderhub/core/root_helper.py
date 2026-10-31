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
"""One password for a whole launcher install or removal.

paru, yay and makepkg ask for root once per pacman call - an AUR package
with AUR dependencies meant a password window for every one of them.
Instead pkexec starts this file once, as root ("serve"), and the helpers
get a stand-in for sudo ("client") that hands each call to it. On the
other distributions PlunderHub runs its own steps (apt-get, dnf...)
through the same stand-in.

The root side only runs the package tools in _TOOLS - and the fixed
system fixes in _ACTIONS, as "plunderhub-action NAME" - and only for
processes PlunderHub itself started (checked through the socket's peer
pid). On its stdin it takes:

    cancel   SIGINT to every running command - what Ctrl+C does.
             PlunderHub only sends it while that is safe: pacman
             always (a transaction finishes the package it is on and
             stops cleanly), the others only while downloading
    wait     answers IDLE once nothing is running any more
    cleanup  waits likewise, then deletes the package files downloaded
             since it started, and answers CLEANED
    EOF      waits for what's running, then exits - also when
             PlunderHub dies. A pacman is interrupted first; an apt or
             dnf transaction is never cut short

Standard library only: it runs as `python3 -I`, as root.
"""

import glob
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from typing import List, Optional

_TOOLS = ("pacman", "apt-get", "dpkg", "add-apt-repository", "debconf-set-selections", "dnf", "rpm", "zypper")
_TOOL_DIRS = ("/usr/bin", "/usr/sbin", "/bin", "/sbin")
# Interrupted whenever their client goes away. The others only on an
# explicit "cancel".
_ALWAYS_INTERRUPTIBLE = ("pacman",)
_ROOT_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
             "HOME": "/root", "TERM": "dumb", "DEBIAN_FRONTEND": "noninteractive"}
_SOCKET_ENV = "PLUNDERHUB_ROOT_SOCKET"
ACTION = "plunderhub-action"
# Shipwright's system fixes. Each takes no arguments and writes only its
# own fixed file, so none of them can be pointed anywhere else.
_ACTIONS = ("enable-multilib", "max-map-count", "load-ntsync", "nofile-limit",
            "enable-lactd", "disable-lactd", "udev-reload", "pacman-tmpdb", "add-flathub")
# The one action with an argument: a Flathub app installed system-wide
# (Flatpak just set up by the same password - Ubuntu ships without it).
# Only an app id, only from Flathub: nothing is run.
FLATPAK_INSTALL = "flatpak-install"
_FLATPAK_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}")
# The databases an Arch update downloads against (as checkupdates does): a
# Cancel then leaves the system's own sync databases as they were - newer
# ones without the upgrade would make every later install a partial upgrade.
PACMAN_TMPDB = "/var/lib/plunderhub-pacman-db"
# Where each package manager keeps what it downloads.
_CACHE_ROOTS = ("/var/cache/apt/archives", "/var/cache/dnf", "/var/cache/libdnf5", "/var/cache/zypp/packages")


# --- root side -----------------------------------------------------------------

def _cache_dirs() -> List[str]:
    dirs = [d for d in _CACHE_ROOTS if os.path.isdir(d)]
    if os.path.exists("/usr/bin/pacman-conf"):
        try:
            out = subprocess.run(["/usr/bin/pacman-conf", "CacheDir"], capture_output=True, text=True,
                                 timeout=10, env=_ROOT_ENV).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        dirs += [d.strip() for d in out.splitlines() if d.strip()] or ["/var/cache/pacman/pkg/"]
    return dirs


def _is_package_file(name: str) -> bool:
    return any(part in name for part in (".pkg.tar", ".deb", ".rpm")) or name.endswith(".part")


def _listing(dirs: List[str]) -> set:
    """Package files (and pacman's download-* directories) under dirs,
    all the way down - dnf and zypper keep one directory per repo."""
    found = set()
    for top in dirs:
        for root, subdirs, files in os.walk(top):
            for d in subdirs:
                if d.startswith("download-"):
                    found.add(os.path.join(root, d))
            found.update(os.path.join(root, f) for f in files if _is_package_file(f))
            subdirs[:] = [d for d in subdirs if not d.startswith("download-")]
    return found


def _resolve_tool(name: str) -> Optional[str]:
    """The tool's real path, for a bare name or for an absolute path
    in one of the system's own directories (makepkg asks for
    /usr/bin/pacman)."""
    if "/" in name:
        directory, base = os.path.split(name)
        if directory not in _TOOL_DIRS or base not in _TOOLS:
            return None
        candidates = [name]
    elif name in _TOOLS:
        candidates = [os.path.join(d, name) for d in _TOOL_DIRS]
    else:
        return None
    return next((c for c in candidates if os.path.isfile(c) and os.access(c, os.X_OK)), None)


def _parent_pid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as f:
            # The command name may contain spaces and parentheses - the
            # fields after it don't.
            return int(f.read().rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _descends_from(pid: int, ancestor: int) -> bool:
    for _ in range(64):
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
        pid = _parent_pid(pid)
    return False


class _Server:
    def __init__(self, sock_path: str, uid: int):
        self.uid = uid
        # pkexec execs this file rather than forking, so the parent is
        # PlunderHub itself.
        self.owner = os.getppid()
        self.procs = set()
        self.lock = threading.Lock()
        self.idle = threading.Condition(self.lock)
        self.cache_dirs = _cache_dirs()
        self.cache_before = _listing(self.cache_dirs)

        parent = os.lstat(os.path.dirname(sock_path))
        if (not os.path.isdir(os.path.dirname(sock_path)) or os.path.islink(os.path.dirname(sock_path))
                or parent.st_uid != uid or parent.st_mode & 0o077):
            raise SystemExit("PlunderHub root helper: unsafe socket directory")
        self.sock_path = sock_path
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            self.server.bind(sock_path)
        finally:
            os.umask(old_umask)
        # never follow a symlink swapped into the user's own directory
        os.chown(sock_path, uid, -1, follow_symlinks=False)
        self.server.listen(8)

    def interrupt(self, only_safe: bool = False):
        with self.lock:
            for proc in self.procs:
                if only_safe and proc.tool not in _ALWAYS_INTERRUPTIBLE:
                    continue
                # Only the tool itself: pacman's hooks and install
                # scriptlets (mkinitcpio, ldconfig...) must never be cut
                # short.
                try:
                    proc.send_signal(signal.SIGINT)
                except OSError:
                    pass

    def wait_idle(self):
        with self.lock:
            while self.procs:
                self.idle.wait()

    def cleanup(self):
        self.wait_idle()
        for path in sorted(_listing(self.cache_dirs) - self.cache_before):
            name = os.path.basename(path)
            try:
                if os.path.islink(path):
                    continue
                if os.path.isdir(path) and name.startswith("download-"):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path) and _is_package_file(name):
                    os.unlink(path)
            except OSError:
                pass

    def accept_loop(self):
        while True:
            try:
                conn, _addr = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn: socket.socket):
        with conn:
            cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, _gid = struct.unpack("3i", cred)
            if uid != self.uid or not _descends_from(pid, self.owner):
                return
            try:
                msg, fds, _flags, _addr = socket.recv_fds(conn, 65536, 3)
            except OSError:
                return
            try:
                if len(fds) != 3:
                    return
                try:
                    argv = json.loads(msg.decode())["argv"]
                except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                    argv = None
                ok = isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
                if ok and argv[0] == ACTION:
                    # this same file, as root, doing one of its own fixes
                    ok = (len(argv) == 2 and argv[1] in _ACTIONS) or \
                        (len(argv) == 3 and argv[1] == FLATPAK_INSTALL and bool(_FLATPAK_ID.fullmatch(argv[2])))
                    tool = sys.executable if ok else None
                    argv = [ACTION, "-I", os.path.abspath(__file__), "action"] + argv[1:] if ok else argv
                else:
                    tool = _resolve_tool(argv[0]) if ok else None
                if tool is None:
                    os.write(fds[2], b"PlunderHub: only package tools can run as root here\n")
                    conn.sendall(b"1\n")
                    return
                stdbuf = next((p for p in ("/usr/bin/stdbuf", "/bin/stdbuf") if os.path.exists(p)), None)
                with self.lock:
                    proc = subprocess.Popen(([stdbuf, "-oL", "-eL"] if stdbuf else []) + [tool] + argv[1:],
                                            stdin=fds[0], stdout=fds[1], stderr=fds[2],
                                            env=_ROOT_ENV, cwd="/", start_new_session=True)
                    proc.tool = os.path.basename(tool)
                    self.procs.add(proc)
            except OSError as e:
                try:
                    os.write(fds[2], f"PlunderHub: {e}\n".encode())
                    conn.sendall(b"1\n")
                except OSError:
                    pass
                return
            finally:
                for fd in fds:
                    os.close(fd)

            def watch():
                # The client going away (killed on Cancel) interrupts
                # its pacman - an apt or dnf only stops on "cancel".
                try:
                    conn.recv(1)
                except OSError:
                    pass
                if proc.poll() is None and proc.tool in _ALWAYS_INTERRUPTIBLE:
                    try:
                        proc.send_signal(signal.SIGINT)
                    except OSError:
                        pass
            threading.Thread(target=watch, daemon=True).start()
            code = proc.wait()
            with self.lock:
                self.procs.discard(proc)
                self.idle.notify_all()
            try:
                conn.sendall(f"{code}\n".encode())
            except OSError:
                pass


def serve(sock_path: str) -> int:
    if os.geteuid() != 0 or "PKEXEC_UID" not in os.environ:
        return 2
    server = _Server(sock_path, int(os.environ["PKEXEC_UID"]))
    threading.Thread(target=server.accept_loop, daemon=True).start()
    print("READY", flush=True)
    try:
        for line in sys.stdin:
            command = line.strip()
            if command == "cancel":
                server.interrupt()
            elif command == "wait":
                server.wait_idle()
                print("IDLE", flush=True)
            elif command == "cleanup":
                server.cleanup()
                print("CLEANED", flush=True)
    finally:
        server.interrupt(only_safe=True)
        server.wait_idle()
        try:
            os.unlink(sock_path)
        except OSError:
            pass
    return 0


# --- the fixed system fixes, run as root ----------------------------------------

def _write(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    print(f"wrote {path}", flush=True)


def _run(argv: List[str]) -> int:
    exe = next((os.path.join(d, argv[0]) for d in _TOOL_DIRS if os.path.isfile(os.path.join(d, argv[0]))), None)
    if exe is None:
        print(f"{argv[0]} not found", flush=True)
        return 1
    print("$ " + " ".join(argv), flush=True)
    return subprocess.run([exe] + argv[1:], env=_ROOT_ENV).returncode


def _enable_multilib() -> int:
    """Uncomments the [multilib] section of /etc/pacman.conf (a copy of
    the old file is kept beside it)."""
    path = "/etc/pacman.conf"
    with open(path) as f:
        lines = f.read().split("\n")
    out, inside, changed = [], False, False
    for line in lines:
        bare = line.strip()
        if bare in ("#[multilib]", "# [multilib]"):
            inside, changed = True, True
            out.append("[multilib]")
            continue
        if inside:
            # the section's own commented settings (#SigLevel = ... may come
            # before #Include = ...) up to a blank line or the next section
            if bare.startswith("#") and "=" in bare and not bare.lstrip("# ").startswith("["):
                out.append(bare.lstrip("# "))
                continue
            inside = False
        out.append(line)
    if not changed:
        print("[multilib] is already switched on, or not in pacman.conf", flush=True)
        return 0
    shutil.copy2(path, path + ".plunderhub-bak")
    _write(path, "\n".join(out))
    return 0


def _pacman_tmpdb() -> int:
    """A fresh PACMAN_TMPDB: the installed packages linked in, the sync
    databases copied (pacman then only fetches what changed). Root-owned,
    in a root-only place: pacman runs as root on it."""
    shutil.rmtree(PACMAN_TMPDB, ignore_errors=True)        # never follows the local link
    os.makedirs(os.path.join(PACMAN_TMPDB, "sync"), mode=0o755)
    os.symlink("/var/lib/pacman/local", os.path.join(PACMAN_TMPDB, "local"))
    for db in glob.glob("/var/lib/pacman/sync/*.db"):
        shutil.copy2(db, os.path.join(PACMAN_TMPDB, "sync"))
    return 0


def run_action(name: str, arg: Optional[str] = None) -> int:
    if os.geteuid() != 0:
        return 2
    if name == FLATPAK_INSTALL:
        if arg is None or not _FLATPAK_ID.fullmatch(arg):
            return 2
        return _run(["flatpak", "install", "--system", "-y", "--noninteractive", "flathub", arg])
    if name not in _ACTIONS or arg is not None:
        return 2
    if name == "add-flathub":
        return _run(["flatpak", "remote-add", "--system", "--if-not-exists", "flathub",
                     "https://dl.flathub.org/repo/flathub.flatpakrepo"])
    if name == "enable-multilib":
        return _enable_multilib()
    if name == "pacman-tmpdb":
        return _pacman_tmpdb()
    if name == "max-map-count":
        # SteamOS's value - what games built for the Steam Deck expect
        _write("/etc/sysctl.d/80-plunderhub-gaming.conf", "vm.max_map_count = 2147483642\n")
        _write("/proc/sys/vm/max_map_count", "2147483642\n")
        return 0
    if name == "load-ntsync":
        _write("/etc/modules-load.d/plunderhub-ntsync.conf", "ntsync\n")
        return _run(["modprobe", "ntsync"])
    if name == "nofile-limit":
        text = "[Manager]\nDefaultLimitNOFILE=1024:1048576\n"
        _write("/etc/systemd/system.conf.d/80-plunderhub-nofile.conf", text)
        _write("/etc/systemd/user.conf.d/80-plunderhub-nofile.conf", text)
        return 0
    if name == "enable-lactd":
        return _run(["systemctl", "enable", "--now", "lactd.service"])
    if name == "disable-lactd":
        _run(["systemctl", "disable", "--now", "lactd.service"])
        return 0
    if name == "udev-reload":
        return _run(["udevadm", "control", "--reload"]) or _run(["udevadm", "trigger"])
    return 2


# --- the sudo stand-in ----------------------------------------------------------

def client(args: List[str]) -> int:
    # sudo's own flags first, if any - and a bare `sudo -v` (paru's
    # SudoLoop) only checks that root is still there.
    while args and args[0].startswith("-"):
        args = args[1:]
    if not args:
        return 0
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(os.environ[_SOCKET_ENV])
        socket.send_fds(sock, [json.dumps({"argv": args}).encode()], [0, 1, 2])
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = sock.recv(16)
            if not chunk:
                break
            reply += chunk
    except (OSError, KeyError) as e:
        print(f"PlunderHub: no root helper ({e})", file=sys.stderr)
        return 1
    finally:
        sock.close()
    try:
        return int(reply.strip())
    except ValueError:
        return 1


# --- PlunderHub's side -----------------------------------------------------------

class RootError(Exception):
    def __init__(self, code: int, tail: List[str]):
        super().__init__(f"root helper exited with {code}")
        self.code = code
        self.tail = tail


class RootSession:
    """start() asks for the password; command() turns a pacman command
    into one that runs through the session, given env."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="plunderhub-root-")
        self.socket = os.path.join(self.dir, "socket")
        self.sudo = os.path.join(self.dir, "sudo")
        with open(self.sudo, "w") as f:
            f.write(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(os.path.abspath(__file__))}"
                    f" client \"$@\"\n")
        os.chmod(self.sudo, 0o700)
        self.env = {_SOCKET_ENV: self.socket}
        self.proc: Optional[subprocess.Popen] = None
        self.ready = False
        self.lock = threading.Lock()
        self.replies = {}
        self.reply_ready = threading.Condition()

    def start(self):
        self.proc = subprocess.Popen(
            ["pkexec", sys.executable, "-I", os.path.abspath(__file__), "serve", self.socket],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1, start_new_session=True)
        tail = []
        for line in self.proc.stdout:
            line = line.rstrip()
            if line == "READY":
                self.ready = True
                threading.Thread(target=self._read_replies, daemon=True).start()
                return
            if line:
                tail = (tail + [line])[-10:]
        raise RootError(self.proc.wait(), tail)

    def _read_replies(self):
        for line in self.proc.stdout:
            line = line.strip()
            with self.reply_ready:
                self.replies[line] = self.replies.get(line, 0) + 1
                self.reply_ready.notify_all()
        with self.reply_ready:
            self.replies[None] = 1
            self.reply_ready.notify_all()

    def _send(self, command: str) -> bool:
        with self.lock:
            if self.proc is None or self.proc.stdin is None or self.proc.stdin.closed:
                return False
            try:
                self.proc.stdin.write(command + "\n")
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def _ask(self, command: str, answer: str):
        if not self._send(command):
            return
        with self.reply_ready:
            while not self.replies.get(answer) and None not in self.replies:
                self.reply_ready.wait()
            if self.replies.get(answer):
                self.replies[answer] -= 1

    def command(self, argv: List[str]) -> List[str]:
        return [self.sudo] + argv

    def interrupt(self):
        """From any thread. Before the password is in, closes its window."""
        proc = self.proc
        if proc is None:
            return
        if self.ready:
            self._send("cancel")
            return
        try:
            proc.terminate()    # still pkexec, waiting for the password
        except OSError:
            pass

    def wait_idle(self):
        self._ask("wait", "IDLE")

    def cleanup(self):
        self._ask("cleanup", "CLEANED")

    def close(self):
        with self.lock:
            if self.proc is not None and self.proc.stdin is not None:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["client"]:
        sys.exit(client(sys.argv[2:]))
    if sys.argv[1:2] == ["serve"] and len(sys.argv) == 3:
        sys.exit(serve(sys.argv[2]))
    if sys.argv[1:2] == ["action"] and len(sys.argv) in (3, 4):
        sys.exit(run_action(*sys.argv[2:]))
    sys.exit(2)
