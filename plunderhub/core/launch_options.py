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

"""Per-game launch options, read from and written back to the launcher
that owns the game.

PlunderHub starts Steam and Heroic games through the launcher itself
(steam://rungameid/..., heroic://launch/...), so an option only takes
effect if it lives in that launcher's own config - which is also what
keeps the two in sync both ways. Everything goes through one launcher-
neutral LaunchOptions model:

  - SteamBackend: the game's "LaunchOptions" string in localconfig.vdf
    and its Proton in config.vdf's CompatToolMapping. Steam rewrites both
    files when it exits, so they're only written while it's closed.
  - HeroicBackend: the fields of GamesConfig/<app_name>.json.

The "text form" is Steam's launch-option syntax for every launcher -
`VAR=value wrapper ... %command% game args` - parsed into the same model,
so the checkboxes and the text always describe the same thing. Anything
not recognised is kept verbatim (custom env vars, unknown wrappers,
unknown gamescope flags) instead of being dropped on the next save."""

import json
import os
import re
import shlex
import shutil
import struct
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.core.models import GameItem
from plunderhub.constants import CONFIG_DIR
from plunderhub.core.persistence import load_config_json, load_json, save_json
from plunderhub.core.prefix_tools import PrefixInfo, find_proton, proton_wine, wine_prefix
from plunderhub.core.scan_paths import HEROIC_CONFIG_ROOTS
from plunderhub.core.steam_heroic import (
    _vdf_get_ci, parse_vdf_text, steam_libraries, steam_localconfig_path, steam_roots,
)
from plunderhub.core.utils import which
from plunderhub.logging_setup import logger


# --- the model ----------------------------------------------------------------

UPSCALE_METHODS = ("", "fsr", "nis", "integer", "stretch")
WINDOW_TYPES = ("", "fullscreen", "borderless")


@dataclass
class GamescopeOptions:
    enabled: bool = False
    game_width: str = ""
    game_height: str = ""
    output_width: str = ""
    output_height: str = ""
    upscale_method: str = ""        # one of UPSCALE_METHODS
    window_type: str = ""           # one of WINDOW_TYPES
    fps_limit: str = ""
    fps_limit_unfocused: str = ""
    force_grab_cursor: bool = False
    additional_options: str = ""


@dataclass
class LaunchOptions:
    proton: Optional[str] = None    # backend-specific id; None = the launcher's default
    native: bool = False            # Steam: run the game's Linux version instead of Proton
    gamemode: bool = False
    mangohud: bool = False
    vkbasalt: bool = False          # ENABLE_VKBASALT - a Vulkan layer, so it works natively too
    fps_limit: str = ""             # DXVK_FRAME_RATE
    esync: bool = True
    fsync: bool = True
    wayland: bool = False
    hdr: bool = False
    fsr: bool = False
    nvapi: bool = False
    proton_log: bool = False
    battleye: bool = False
    eac: bool = False
    env: Dict[str, str] = field(default_factory=dict)
    wrappers: List[str] = field(default_factory=list)   # unrecognised wrapper words, kept as-is
    args: str = ""
    gamescope: GamescopeOptions = field(default_factory=GamescopeOptions)
    # the text it was read from (parse_command_line): written back as it was
    # while it still says the same - see keep_raw_line
    raw: Optional[str] = field(default=None, compare=False, repr=False)


# Environment variables that are really one of the switches above.
_FLAG_ENV = {
    "PROTON_ENABLE_WAYLAND": "wayland",
    "PROTON_ENABLE_HDR": "hdr",
    "WINE_FULLSCREEN_FSR": "fsr",
    "PROTON_ENABLE_NVAPI": "nvapi",
    "PROTON_LOG": "proton_log",
    "ENABLE_VKBASALT": "vkbasalt",
}
_OFF_FLAG_ENV = {"PROTON_NO_ESYNC": "esync", "PROTON_NO_FSYNC": "fsync"}
_ENV_WORD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


# --- the game's own arguments the dialog switches ----------------------------------

# Window modes many games take on their command line (Unity, Unreal,
# Source...) - not every game knows them. One at most.
WINDOW_ARGS: Dict[str, Tuple[str, ...]] = {"fullscreen": ("-fullscreen",),
                                           "borderless": ("-windowed", "-borderless")}
# Skips a game's own launcher (Paradox, some Ubisoft and EA games)
LAUNCHER_SKIP_ARG = "--launcher-skip"


def _join(words: List[str]) -> str:
    return " ".join(shlex.quote(w) for w in words)


def window_mode_in(args: str) -> Optional[str]:
    """fullscreen or borderless when args ask for it, else None."""
    words = _split(args)
    if "-borderless" in words:
        return "borderless"
    if "-fullscreen" in words:
        return "fullscreen"
    return None


def with_window_mode(args: str, mode: Optional[str]) -> str:
    """args with the window mode's words in place of any other's."""
    every = {w for words in WINDOW_ARGS.values() for w in words}
    words = [w for w in _split(args) if w not in every]
    return _join(words + list(WINDOW_ARGS.get(mode or "", ())))


def with_arg(args: str, word: str, on: bool) -> str:
    """args with word added at the end, or taken out."""
    words = [w for w in _split(args) if w != word]
    return _join(words + [word] if on else words)


# --- text form (Steam launch-option syntax) -------------------------------------

def _split(text: str) -> List[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _take_value(words: List[str], i: int, flag: str) -> Tuple[Optional[str], int]:
    """The value of `flag` at words[i], given as "-w 1920", "--x=1920" or
    "--x 1920"; returns (value, next index)."""
    word = words[i]
    if word.startswith("--") and "=" in word:
        return word.split("=", 1)[1], i + 1
    if i + 1 < len(words):
        return words[i + 1], i + 2
    return None, i + 1


_GS_VALUE_FLAGS = {
    "-w": "game_width", "--nested-width": "game_width",
    "-h": "game_height", "--nested-height": "game_height",
    "-W": "output_width", "--output-width": "output_width",
    "-H": "output_height", "--output-height": "output_height",
    "-r": "fps_limit", "--nested-refresh": "fps_limit",
    "-o": "fps_limit_unfocused", "--nested-unfocused-refresh": "fps_limit_unfocused",
}


def _parse_gamescope(words: List[str], opts: LaunchOptions) -> None:
    gs = GamescopeOptions(enabled=True)
    extra: List[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        name = word.split("=", 1)[0] if word.startswith("--") else word
        if name in _GS_VALUE_FLAGS:
            value, i = _take_value(words, i, name)
            if value is not None:
                setattr(gs, _GS_VALUE_FLAGS[name], value)
            continue
        if name in ("-F", "--filter", "-S", "--scaler"):
            value, nxt = _take_value(words, i, name)
            if value in UPSCALE_METHODS and value and not gs.upscale_method:
                gs.upscale_method = value
                i = nxt
                continue
            extra.extend(words[i:nxt])
            i = nxt
            continue
        if word in ("-f", "--fullscreen"):
            gs.window_type = "fullscreen"
        elif word in ("-b", "--borderless"):
            gs.window_type = "borderless"
        elif word == "--force-grab-cursor":
            gs.force_grab_cursor = True
        elif word == "--mangoapp":
            opts.mangohud = True
        else:
            extra.append(word)
        i += 1
    gs.additional_options = shlex.join(extra) if extra else ""
    opts.gamescope = gs


def parse_command_line(text: str, base: Optional[LaunchOptions] = None) -> LaunchOptions:
    """The text form -> a model. Fields the text can't express (Proton,
    the anti-cheat runtimes) are carried over from `base`."""
    base = base or LaunchOptions()
    opts = LaunchOptions(proton=base.proton, native=base.native, battleye=base.battleye, eac=base.eac)
    opts.raw = text.strip()
    words = _split(text.strip())
    if "%command%" not in words:
        # Steam's own rule: without %command% the whole string is
        # appended to the game's command line as arguments.
        opts.args = text.strip()
        return opts
    split_at = words.index("%command%")
    before, after = words[:split_at], words[split_at + 1:]
    opts.args = shlex.join(after) if after else ""

    i = 0
    while i < len(before) and _ENV_WORD_RE.match(before[i]):
        key, value = _ENV_WORD_RE.match(before[i]).groups()
        if key in _FLAG_ENV and value == "1":
            setattr(opts, _FLAG_ENV[key], True)
        elif key in _OFF_FLAG_ENV and value == "1":
            setattr(opts, _OFF_FLAG_ENV[key], False)
        elif key == "DXVK_FRAME_RATE" and value.isdigit():
            opts.fps_limit = value
        elif key == "MANGOHUD" and value == "1":
            opts.mangohud = True
        else:
            opts.env[key] = value
        i += 1

    rest = before[i:]
    while rest:
        word = rest.pop(0)
        base_name = os.path.basename(word)
        if base_name == "gamemoderun":
            opts.gamemode = True
        elif base_name == "mangohud":
            opts.mangohud = True
        elif base_name == "gamescope":
            end = rest.index("--") if "--" in rest else len(rest)
            _parse_gamescope(rest[:end], opts)
            rest = rest[end + 1:]
        else:
            opts.wrappers.append(word)
    return opts


def gamescope_args(gs: GamescopeOptions, mangoapp: bool = False) -> List[str]:
    args: List[str] = []
    for flag, value in (("-w", gs.game_width), ("-h", gs.game_height),
                        ("-W", gs.output_width), ("-H", gs.output_height)):
        if value:
            args += [flag, value]
    if gs.upscale_method in ("fsr", "nis"):
        args += ["-F", gs.upscale_method]
    elif gs.upscale_method in ("integer", "stretch"):
        args += ["-S", gs.upscale_method]
    if gs.window_type == "fullscreen":
        args.append("-f")
    elif gs.window_type == "borderless":
        args.append("-b")
    if gs.fps_limit:
        args += ["-r", gs.fps_limit]
    if gs.fps_limit_unfocused:
        args += ["-o", gs.fps_limit_unfocused]
    if gs.force_grab_cursor:
        args.append("--force-grab-cursor")
    if mangoapp:
        args.append("--mangoapp")
    args += _split(gs.additional_options)
    return args


def env_for(opts: LaunchOptions) -> Dict[str, str]:
    """Every environment variable the model implies, switches included."""
    env: Dict[str, str] = {}
    if not opts.esync:
        env["PROTON_NO_ESYNC"] = "1"
    if not opts.fsync:
        env["PROTON_NO_FSYNC"] = "1"
    for key, attr in _FLAG_ENV.items():
        if getattr(opts, attr):
            env[key] = "1"
    if opts.fps_limit:
        env["DXVK_FRAME_RATE"] = opts.fps_limit
    env.update(opts.env)
    return env


def to_command_line(opts: LaunchOptions) -> str:
    """The model -> the text form ("" when there's nothing to say)."""
    parts = [f"{k}={shlex.quote(v)}" for k, v in env_for(opts).items()]
    if opts.gamemode:
        parts.append("gamemoderun")
    if opts.gamescope.enabled:
        # MangoHud inside gamescope goes through --mangoapp (as Heroic does).
        parts += ["gamescope", *map(shlex.quote, gamescope_args(opts.gamescope, opts.mangohud)), "--"]
    elif opts.mangohud:
        parts.append("mangohud")
    parts += [shlex.quote(w) for w in opts.wrappers]
    if not parts and not opts.args:
        return ""
    parts.append("%command%")
    if opts.args:
        parts.append(opts.args)
    return " ".join(parts)


def keep_raw_line(opts: LaunchOptions, *raws: Optional[str]) -> str:
    """The line to write for opts: the first of raws (the user's own text)
    that still means the same, else the line built from the model. Steam
    runs the line through a shell; the model keeps only its words, so a
    rebuilt `$(...)`, `|`, `> log`, `$HOME` or a quoted %command% would
    break - a change of Proton alone mustn't touch it."""
    line = to_command_line(opts)
    for raw in raws:
        if raw and to_command_line(parse_command_line(raw)) == line:
            return raw
    return line


# --- gamescope resolution presets -------------------------------------------------

# AMD FSR's standard quality modes: the game renders at the output
# resolution divided by the factor, and gamescope upscales it back.
QUALITY_PRESETS: Tuple[Tuple[str, float], ...] = (
    ("native", 1.0),
    ("ultra_quality", 1.3),
    ("quality", 1.5),
    ("balanced", 1.7),
    ("performance", 2.0),
    ("ultra_performance", 3.0),
)


def preset_game_resolution(output_w: int, output_h: int, factor: float) -> Tuple[int, int]:
    """The render resolution for a preset, rounded to even numbers (what
    upscalers and video encoders expect)."""
    return (max(2, round(output_w / factor / 2) * 2), max(2, round(output_h / factor / 2) * 2))


def detect_preset(game_w: str, game_h: str, output_w: str, output_h: str) -> Optional[str]:
    """Which preset the four resolution fields amount to, if any."""
    try:
        gw, gh, ow, oh = (int(v) for v in (game_w, game_h, output_w, output_h))
    except ValueError:
        return None
    for name, factor in QUALITY_PRESETS:
        pw, ph = preset_game_resolution(ow, oh, factor)
        if abs(pw - gw) <= 2 and abs(ph - gh) <= 2:
            return name
    return None


def drm_connected_modes(drm_dir: Path = Path("/sys/class/drm")) -> List[Tuple[int, int]]:
    """The preferred (first-listed) mode of every connected display, in
    real pixels - unlike GTK3's monitor geometry, which is in logical
    pixels under fractional scaling (e.g. 2048x1152 for 2560x1440 at 125%)."""
    modes: List[Tuple[int, int]] = []
    try:
        connectors = sorted(drm_dir.glob("card*-*"))
    except OSError:
        return modes
    for connector in connectors:
        try:
            if (connector / "status").read_text().strip() != "connected":
                continue
            first = (connector / "modes").read_text().splitlines()[0]
        except (OSError, IndexError):
            continue
        m = re.match(r"^(\d+)x(\d+)", first.strip())
        if m:
            modes.append((int(m.group(1)), int(m.group(2))))
    return modes


# --- tool availability ----------------------------------------------------------

def missing_tools() -> Dict[str, bool]:
    """Which tools the switches need aren't installed on the host (for
    native launchers; a Flatpak launcher needs them inside its sandbox
    instead). Shipwright installs them."""
    from plunderhub.core.tools import TOOLS_BY_ID, installed_path
    return {tool: installed_path(TOOLS_BY_ID[tool]) is None
            for tool in ("gamemode", "mangohud", "gamescope", "vkbasalt")}


def _process_running(names: Tuple[str, ...]) -> bool:
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
                if f.read().strip() in names:
                    return True
        except OSError:
            continue
    return False


# --- minimal round-trip VDF editing ----------------------------------------------

_Token = Tuple[str, str, int, int]   # (kind, value, start, end); kind: "str", "{", "}"


def _vdf_tokens(text: str) -> List[_Token]:
    tokens: List[_Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif c in "{}":
            tokens.append((c, c, i, i + 1))
            i += 1
        elif c == '"':
            j, out = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    out.append(text[j + 1])
                    j += 2
                    continue
                out.append(text[j])
                j += 1
            tokens.append(("str", "".join(out), i, min(j + 1, n)))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n{}"':
                j += 1
            tokens.append(("str", text[i:j], i, j))
            i = j
    return tokens


def _vdf_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _VdfBlock:
    """One { ... } block: its direct children, with token positions."""

    def __init__(self, tokens: List[_Token], open_idx: int):
        self.tokens = tokens
        self.open_idx = open_idx            # index of "{" (-1 for the document)
        self.children: List[Tuple[int, int, Optional[int]]] = []   # (key idx, value idx, close idx|None)
        i = open_idx + 1
        while i < len(tokens) and tokens[i][0] != "}":
            if tokens[i][0] != "str" or i + 1 >= len(tokens):
                i += 1
                continue
            if tokens[i + 1][0] == "{":
                close = _matching_close(tokens, i + 1)
                self.children.append((i, i + 1, close))
                i = close + 1
            elif tokens[i + 1][0] == "str":
                self.children.append((i, i + 1, None))
                i += 2
            else:
                i += 1
        self.close_idx = i                  # index of "}" (or len(tokens) for the document)

    def find(self, key: str) -> Optional[Tuple[int, int, Optional[int]]]:
        for child in self.children:
            if self.tokens[child[0]][1].lower() == key.lower():
                return child
        return None


def _matching_close(tokens: List[_Token], open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(tokens)):
        if tokens[i][0] == "{":
            depth += 1
        elif tokens[i][0] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(tokens)


def _line_indent(text: str, pos: int) -> str:
    """The whitespace that starts the line holding pos."""
    line = text[text.rfind("\n", 0, pos) + 1:pos]
    return line[: len(line) - len(line.lstrip("\t "))]


def _insert_before_close(text: str, tokens: List[_Token], block: _VdfBlock, snippet_lines: List[str]) -> str:
    if block.close_idx < len(tokens):
        close_pos = tokens[block.close_idx][2]
        indent = _line_indent(text, close_pos)
        line_start = text.rfind("\n", 0, close_pos) + 1
        body = "".join(f"{indent}\t{line}\n" for line in snippet_lines)
        return text[:line_start] + body + text[line_start:]
    sep = "" if text.endswith("\n") or not text else "\n"
    return text + sep + "".join(f"{line}\n" for line in snippet_lines)


def vdf_set(text: str, path: List[str], key: str, value: str) -> str:
    """Sets path/key to value, creating missing blocks, touching nothing
    else. The path starts below the document's single root block."""
    tokens = _vdf_tokens(text)
    doc = _VdfBlock(tokens, -1)
    if not doc.children or doc.children[0][2] is None:
        raise ValueError("not a VDF document")
    block = _VdfBlock(tokens, doc.children[0][1])
    for depth, name in enumerate(path):
        child = block.find(name)
        if child is None or child[2] is None:
            rest = path[depth:]
            lines: List[str] = []
            for level, sub in enumerate(rest):
                lines += ["\t" * level + _vdf_quote(sub), "\t" * level + "{"]
            lines.append("\t" * len(rest) + f"{_vdf_quote(key)}\t\t{_vdf_quote(value)}")
            lines += ["\t" * level + "}" for level in reversed(range(len(rest)))]
            return _insert_before_close(text, tokens, block, lines)
        block = _VdfBlock(tokens, child[1])
    existing = block.find(key)
    if existing is not None and existing[2] is None:
        _, _, start, end = tokens[existing[1]]
        return text[:start] + _vdf_quote(value) + text[end:]
    return _insert_before_close(text, tokens, block, [f"{_vdf_quote(key)}\t\t{_vdf_quote(value)}"])


def vdf_remove(text: str, path: List[str], key: str) -> str:
    """Removes path/key (a value or a whole block); a no-op if it's absent."""
    tokens = _vdf_tokens(text)
    doc = _VdfBlock(tokens, -1)
    if not doc.children or doc.children[0][2] is None:
        return text
    block = _VdfBlock(tokens, doc.children[0][1])
    for name in path:
        child = block.find(name)
        if child is None or child[2] is None:
            return text
        block = _VdfBlock(tokens, child[1])
    found = block.find(key)
    if found is None:
        return text
    key_idx, value_idx, close_idx = found
    start = text.rfind("\n", 0, tokens[key_idx][2]) + 1
    end_tok = tokens[close_idx if close_idx is not None else value_idx]
    end = text.find("\n", end_tok[3])
    end = len(text) if end == -1 else end + 1
    return text[:start] + text[end:]


def _write_with_backup(path: Path, text: str) -> None:
    backup = path.with_name(path.name + ".plunderhub-bak")
    try:
        shutil.copy2(path, backup)
    except OSError as e:
        logger.warning(f"Couldn't back up {path} before editing it: {e}")
    tmp = path.with_name(f".{path.name}.plunderhub-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --- backends -------------------------------------------------------------------

@dataclass
class ProtonChoice:
    id: str
    label: str


# Dialog widgets a backend can switch off (see LaunchOptionsBackend.unsupported).
WRAPPER_KEY = "wrappers"


class LaunchOptionsBackend:
    launcher = ""
    # "toggles": per-game BattlEye/EAC switches; "steam": runtime status
    # only (Steam uses them automatically); None: the launcher has neither.
    anticheat_mode: Optional[str] = "toggles"
    # Upscaling methods the launcher can store for gamescope.
    upscale_methods: Tuple[str, ...] = UPSCALE_METHODS
    # An i18n key shown as a notice in the dialog, if any.
    note_key: Optional[str] = None
    # PlunderHub builds this launcher's command itself, so a rescan picks
    # up saved changes.
    needs_rescan = False

    def where(self, game: GameItem) -> str:
        raise NotImplementedError

    def load(self, game: GameItem) -> LaunchOptions:
        raise NotImplementedError

    def save(self, game: GameItem, opts: LaunchOptions) -> None:
        raise NotImplementedError

    def proton_choices(self, game: GameItem, current: Optional[str]) -> List[ProtonChoice]:
        return []

    def native_capable(self, game: GameItem) -> bool:
        """Whether the game also has a Linux version to run instead of
        Proton (only Steam can switch between the two after install)."""
        return False

    def prefix(self, game: GameItem) -> Optional[PrefixInfo]:
        """The game's Wine prefix and what runs it, for Launch options'
        winecfg / Winetricks / Run EXE (see core/prefix_tools.py) - None
        where the launcher has none."""
        return None

    def unsupported(self, game: GameItem) -> frozenset:
        """Dialog widget keys this launcher can't store for this game."""
        return frozenset({"battleye", "eac"}) if self.anticheat_mode is None else frozenset()

    def blocking_process_running(self) -> bool:
        """True when the launcher must be closed before saving."""
        return False

    def advisory_process_running(self) -> bool:
        """True when the launcher runs but saving is still safe."""
        return False

    def running(self) -> bool:
        """Whether the launcher itself runs - shown, with a way to close
        it, on the game's Launch options."""
        return bool(launcher_pids(self.launcher))


# Steam --------------------------------------------------------------------------

_STEAM_APPS_PATH = ["Software", "Valve", "Steam", "apps"]
_STEAM_COMPAT_PATH = ["Software", "Valve", "Steam", "CompatToolMapping"]
_STEAM_RUNTIME_DIRS = {"eac": "Proton EasyAntiCheat Runtime", "battleye": "Proton BattlEye Runtime"}
# Where distro packages put Proton builds (e.g. proton-ge-custom, CachyOS's proton-cachyos).
SYSTEM_COMPAT_TOOL_DIRS = [Path("/usr/share/steam/compatibilitytools.d"),
                           Path("/usr/local/share/steam/compatibilitytools.d")]


def _steam_builtin_proton_id(dir_name: str) -> Optional[str]:
    """Steam's internal name for one of its own Proton folders:
    "Proton 9.0" -> proton_9, "Proton 5.13" -> proton_513,
    "Proton - Experimental" -> proton_experimental."""
    low = dir_name.lower()
    if low == "proton - experimental":
        return "proton_experimental"
    if low == "proton hotfix":
        return "proton_hotfix"
    m = re.match(r"^proton (\d+)\.(\d+)", low)
    if not m:
        return None
    major, minor = m.groups()
    return f"proton_{major}" if minor == "0" else f"proton_{major}{minor}"


_vdf_cache: Dict[str, Tuple[int, int, dict]] = {}
_vdf_cache_lock = threading.Lock()


def _parsed_vdf(path: Path) -> dict:
    """A text VDF parsed once while it stays unchanged: a scan's upkeep asks
    for every game's options, and a big localconfig.vdf takes a while.
    The answer is shared - read it, never change it."""
    st = path.stat()
    key = str(path)
    with _vdf_cache_lock:
        hit = _vdf_cache.get(key)
        if hit is not None and hit[:2] == (st.st_mtime_ns, st.st_size):
            return hit[2]
    data = parse_vdf_text(path.read_text(encoding="utf-8", errors="ignore"))
    with _vdf_cache_lock:
        _vdf_cache[key] = (st.st_mtime_ns, st.st_size, data)
    return data


def _compat_tools_in(directory: Path) -> List[ProtonChoice]:
    choices = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    for entry in entries:
        vdf = entry / "compatibilitytool.vdf"
        if not vdf.is_file():
            continue
        try:
            data = _parsed_vdf(vdf)
        except OSError:
            continue
        tools = _vdf_get_ci(data, "compat_tools")
        if not isinstance(tools, dict):
            continue
        for internal, info in tools.items():
            label = _vdf_get_ci(info, "display_name") if isinstance(info, dict) else None
            choices.append(ProtonChoice(internal, str(label or internal)))
    return choices


_APPINFO_MAGICS = {0x07564427: 27, 0x07564428: 28, 0x07564429: 29}
_APPINFO_CACHE: Dict[str, Tuple[Tuple[int, int], Dict[str, str]]] = {}


def read_appinfo_oslists(path: Path, appids) -> Dict[str, str]:
    """appinfo/common/oslist ("windows,linux", ...) of the given apps, from
    Steam's binary appcache/appinfo.vdf (formats 27-29; 29 keeps keys in a
    string table). Other apps are skipped by their size field unparsed."""
    wanted = {int(a) for a in appids}
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    cached = _APPINFO_CACHE.get(str(path))
    if cached and cached[0] == stamp and all(str(a) in cached[1] for a in wanted):
        return {str(a): cached[1][str(a)] for a in wanted}
    data = path.read_bytes()
    magic, _universe = struct.unpack_from("<II", data, 0)
    version = _APPINFO_MAGICS.get(magic)
    if version is None:
        raise ValueError(f"unknown appinfo.vdf format {magic:#x}")
    pos, strings = 8, None
    if version >= 29:
        (table_at,) = struct.unpack_from("<q", data, 8)
        pos = 16
        (count,) = struct.unpack_from("<I", data, table_at)
        p, strings = table_at + 4, []
        for _ in range(count):
            end = data.index(b"\0", p)
            strings.append(data[p:end].decode("utf-8", "replace"))
            p = end + 1

    def cstr(p: int) -> Tuple[str, int]:
        end = data.index(b"\0", p)
        return data[p:end].decode("utf-8", "replace"), end + 1

    def read_kv(p: int, limit: int) -> Tuple[Dict, int]:
        out: Dict = {}
        while p < limit:
            kind = data[p]
            p += 1
            if kind == 0x08:
                return out, p
            if strings is not None:
                (idx,) = struct.unpack_from("<I", data, p)
                p += 4
                key = strings[idx]
            else:
                key, p = cstr(p)
            if kind == 0x00:
                value, p = read_kv(p, limit)
            elif kind == 0x01:
                value, p = cstr(p)
            elif kind in (0x02, 0x04, 0x06):
                (value,) = struct.unpack_from("<i", data, p)
                p += 4
            elif kind == 0x03:
                (value,) = struct.unpack_from("<f", data, p)
                p += 4
            elif kind in (0x07, 0x0A):
                (value,) = struct.unpack_from("<Q", data, p)
                p += 8
            else:
                raise ValueError(f"unknown appinfo value type {kind:#x}")
            out[key] = value
        return out, p

    header = 60 if version >= 28 else 40     # state, times, token, sha1, change, (binary sha1)
    found: Dict[str, str] = dict(cached[1]) if cached and cached[0] == stamp else {}
    while pos + 8 <= len(data):
        appid, size = struct.unpack_from("<II", data, pos)
        if appid == 0:
            break
        start, end = pos + 8, pos + 8 + size
        if appid in wanted:
            info, _ = read_kv(start + header, end)
            common = info.get("appinfo", {}).get("common", {}) if isinstance(info.get("appinfo"), dict) else {}
            found[str(appid)] = str(common.get("oslist", "")) if isinstance(common, dict) else ""
        pos = end
    for appid in wanted:
        found.setdefault(str(appid), "")
    _APPINFO_CACHE[str(path)] = (stamp, found)
    return {str(a): found[str(a)] for a in wanted}


# PlunderHub's own record of the Proton-by-default choice for Steam games
# that also have a Linux version: "native" - the person ticked "Run
# natively"; "defaulted" - Proton was set for them once already, so a
# later switch back in Steam itself is respected, not overridden again.
STEAM_NATIVE_FILE = CONFIG_DIR / "steam_native_games.json"


def _load_native_record() -> Dict[str, List[str]]:
    data = load_config_json(STEAM_NATIVE_FILE)
    data = data if isinstance(data, dict) else {}
    return {key: [str(a) for a in data.get(key, []) if str(a).isdigit()] for key in ("native", "defaulted")}


def _save_native_record(record: Dict[str, List[str]]) -> None:
    STEAM_NATIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    save_json({k: sorted(set(v), key=int) for k, v in record.items()}, STEAM_NATIVE_FILE)


class SteamBackend(LaunchOptionsBackend):
    launcher = "Steam"
    anticheat_mode = "steam"     # Steam uses the runtimes on its own

    def _localconfig(self) -> Path:
        path = steam_localconfig_path()
        if path is None:
            raise FileNotFoundError("Steam's localconfig.vdf wasn't found")
        return path

    def _config_vdf(self) -> Path:
        # userdata/<id>/config/localconfig.vdf -> <steam root>/config/config.vdf
        return self._localconfig().parents[3] / "config" / "config.vdf"

    @staticmethod
    def _appid(game: GameItem) -> str:
        return game.uid.split(":", 1)[1]

    def where(self, game: GameItem) -> str:
        try:
            return str(self._localconfig())
        except FileNotFoundError:
            return "localconfig.vdf"

    def _appinfo(self) -> Path:
        return self._localconfig().parents[3] / "appcache" / "appinfo.vdf"

    def prefix(self, game: GameItem) -> Optional[PrefixInfo]:
        appid = self._appid(game)
        info = PrefixInfo("steam", appid=appid, runner="Proton")
        try:
            if self.native_capable(game) and self.load(game).native:
                info.problem = "pfx_native"
                return info
        except (OSError, ValueError):
            pass
        for lib in steam_libraries():
            found = wine_prefix(lib / "steamapps" / "compatdata" / appid)
            if found:
                info.prefix = found
                break
        else:
            # Steam makes it the first time the game starts with Proton
            info.problem = "pfx_missing"
        return info

    def native_capable(self, game: GameItem) -> bool:
        return bool(self.native_capable_appids([self._appid(game)]))

    def native_capable_appids(self, appids) -> set:
        try:
            oslists = read_appinfo_oslists(self._appinfo(), appids)
        except (OSError, ValueError, struct.error) as e:
            logger.debug(f"Couldn't read Steam's appinfo.vdf: {e}")
            return set()
        return {a for a, oslist in oslists.items() if "linux" in oslist.lower().split(",")}

    def _compat_mapping(self) -> Dict:
        config = self._config_vdf()
        if not config.is_file():
            return {}
        cdata = _parsed_vdf(config)
        mapping = _vdf_get_ci(cdata, *_STEAM_COMPAT_PATH)
        return mapping if isinstance(mapping, dict) else {}

    def _mapped_tool(self, mapping: Dict, appid: str) -> Optional[str]:
        entry = _vdf_get_ci(mapping, appid)
        tool = _vdf_get_ci(entry, "name") if isinstance(entry, dict) else None
        return tool if isinstance(tool, str) and tool else None

    def default_tool(self) -> str:
        """The Proton set as Steam Play's default (the "0" mapping), else the
        newest built-in one installed."""
        tool = self._mapped_tool(self._compat_mapping(), "0")
        if tool:
            return tool
        builtin = [c.id for c in self.proton_choices(None, None) if c.id.startswith("proton_")]
        numbered = sorted((c for c in builtin if c[len("proton_"):].isdigit()), key=lambda c: int(c[7:]))
        return numbered[-1] if numbered else "proton_experimental"

    def load(self, game: GameItem) -> LaunchOptions:
        appid = self._appid(game)
        data = _parsed_vdf(self._localconfig())
        raw = _vdf_get_ci(data, *_STEAM_APPS_PATH, appid, "LaunchOptions")
        opts = parse_command_line(raw if isinstance(raw, str) else "")
        opts.proton = self._mapped_tool(self._compat_mapping(), appid)
        if opts.proton is None and self.native_capable(game):
            record = _load_native_record()
            # Unmapped means native in Steam - but until Proton was set for
            # it once by default, it's simply waiting for that default.
            opts.native = appid in record["native"] or appid in record["defaulted"]
        return opts

    def save(self, game: GameItem, opts: LaunchOptions) -> None:
        from plunderhub.core.file_lock import held
        with held(self._localconfig()), held(self._config_vdf()):
            self._save(game, opts)

    def _save(self, game: GameItem, opts: LaunchOptions) -> None:
        if self.blocking_process_running():
            raise RuntimeError("Steam is running")
        appid = self._appid(game)
        native_capable = self.native_capable(game)
        tool = None if opts.native and native_capable else opts.proton
        if tool is None and native_capable and not opts.native:
            tool = self.default_tool()      # Proton by default; native only when ticked
        if tool and not self._config_vdf().is_file():
            # before localconfig.vdf is touched: no half-saved change
            raise FileNotFoundError(f"{self._config_vdf()} wasn't found")
        local = self._localconfig()
        text = local.read_text(encoding="utf-8", errors="ignore")
        current = _vdf_get_ci(parse_vdf_text(text), *_STEAM_APPS_PATH, appid, "LaunchOptions")
        line = keep_raw_line(opts, opts.raw, current if isinstance(current, str) else None)
        if line:
            new_text = vdf_set(text, [*_STEAM_APPS_PATH, appid], "LaunchOptions", line)
        else:
            new_text = vdf_remove(text, [*_STEAM_APPS_PATH, appid], "LaunchOptions")
        if new_text != text:
            _write_with_backup(local, new_text)
        self._set_compat_tools({appid: tool})
        if native_capable:
            record = _load_native_record()
            record["native"] = [a for a in record["native"] if a != appid] + ([appid] if opts.native else [])
            record["defaulted"] = [a for a in record["defaulted"] if a != appid] + [appid]
            _save_native_record(record)

    def _set_compat_tools(self, tools: Dict[str, Optional[str]]) -> None:
        """appid -> Proton (None removes the game's own mapping), in one edit."""
        from plunderhub.core.file_lock import held
        with held(self._config_vdf()):
            self._set_compat_tools_locked(tools)

    def _set_compat_tools_locked(self, tools: Dict[str, Optional[str]]) -> None:
        config = self._config_vdf()
        if not config.is_file():
            if any(tools.values()):
                raise FileNotFoundError(f"{config} wasn't found")
            return
        ctext = config.read_text(encoding="utf-8", errors="ignore")
        new_ctext = ctext
        for appid, tool in tools.items():
            if tool:
                for key, value in (("name", tool), ("config", ""), ("priority", "250")):
                    new_ctext = vdf_set(new_ctext, [*_STEAM_COMPAT_PATH, appid], key, value)
            else:
                new_ctext = vdf_remove(new_ctext, _STEAM_COMPAT_PATH, appid)
        if new_ctext != ctext:
            _write_with_backup(config, new_ctext)

    def apply_default_proton(self, appids) -> List[str]:
        """Sets Steam Play's default Proton for games that also have a Linux
        version and haven't been handled yet - once each, and only while
        Steam is closed (it would overwrite config.vdf). Windows-only games
        need nothing: Steam already runs them with the default Proton.
        Returns the appids that were switched."""
        appids = [str(a) for a in appids]
        if not appids or self.blocking_process_running():
            return []
        record = _load_native_record()
        handled = set(record["native"]) | set(record["defaulted"])
        candidates = [a for a in appids if a not in handled]
        if not candidates:
            return []
        try:
            mapping = self._compat_mapping()
        except OSError:
            return []
        capable = self.native_capable_appids(candidates)
        switch = [a for a in candidates if a in capable and self._mapped_tool(mapping, a) is None]
        if switch:
            tool = self.default_tool()
            self._set_compat_tools({a: tool for a in switch})
            logger.info(f"Set {tool} as the default for {len(switch)} Steam game(s) that also have "
                        f"a Linux version: {', '.join(switch)}")
        # Already mapped (or Windows-only) ones count as handled, too.
        record["defaulted"] = sorted(set(record["defaulted"]) | set(candidates), key=int)
        _save_native_record(record)
        return switch

    def proton_choices(self, game: GameItem, current: Optional[str]) -> List[ProtonChoice]:
        choices: List[ProtonChoice] = []
        for lib in sorted(steam_libraries(), key=str):
            try:
                entries = sorted((lib / "steamapps" / "common").iterdir())
            except OSError:
                continue
            for entry in entries:
                internal = _steam_builtin_proton_id(entry.name)
                if internal and (entry / "proton").exists():
                    choices.append(ProtonChoice(internal, entry.name))
        tool_dirs = [root / "compatibilitytools.d" for root in steam_roots()] + SYSTEM_COMPAT_TOOL_DIRS
        for directory in tool_dirs:
            choices += _compat_tools_in(directory)
        unique: Dict[str, ProtonChoice] = {}
        for choice in choices:
            unique.setdefault(choice.id, choice)
        if current and current not in unique:
            unique[current] = ProtonChoice(current, current)
        return list(unique.values())

    def runtime_installed(self, which: str) -> bool:
        name = _STEAM_RUNTIME_DIRS[which]
        return any((lib / "steamapps" / "common" / name).is_dir() for lib in steam_libraries())

    def blocking_process_running(self) -> bool:
        return _process_running(("steam", "steamwebhelper"))


_LAUNCHER_COMMS = {
    "Steam": ("steam",),
    "Lutris": ("lutris",),
    "Faugus": ("faugus-launcher", "faugus-launche"),
    "Bottles": ("bottles",),
}


def launcher_pids(launcher: str) -> List[int]:
    """The launcher's own main processes - native or Flatpak, both run
    under the same name (Heroic's are found by heroic_main_pids)."""
    if launcher == "Heroic":
        from plunderhub.core.heroic_launch import heroic_main_pids
        return heroic_main_pids()
    names = _LAUNCHER_COMMS.get(launcher)
    if not names:
        return []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    found = []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
                if f.read().strip() in names:
                    found.append(int(pid))
        except OSError:
            continue
    return found


def steam_shutdown_invocations() -> List[List[str]]:
    """How to reach each Steam that's running: the Flatpak's processes
    sit in its own app-flatpak-com.valvesoftware.Steam-* cgroup."""
    from plunderhub.core.scan_paths import LAUNCHER_FLATPAK_IDS
    from plunderhub.core.utils import which
    flatpak = native = False
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
                if f.read().strip() != "steam":
                    continue
            with open(f"/proc/{pid}/cgroup", encoding="utf-8", errors="replace") as f:
                cgroup = f.read()
        except OSError:
            continue
        flatpak_id = next((i for i in LAUNCHER_FLATPAK_IDS["Steam"] if f"app-flatpak-{i}" in cgroup), None)
        if flatpak_id:
            flatpak = flatpak_id
        else:
            native = True
    found = []
    if native and which("steam"):
        found.append([which("steam")])
    if flatpak:
        found.append(["flatpak", "run", flatpak])
    return found


# Heroic -------------------------------------------------------------------------

_HEROIC_FLAG_FIELDS = {
    "gamemode": "useGameMode",
    "mangohud": "showMangohud",
    "esync": "enableEsync",
    "fsync": "enableFsync",
    "wayland": "enableWineWayland",
    "hdr": "enableHDR",
    "nvapi": "autoInstallDxvkNvapi",
    "battleye": "battlEyeRuntime",
    "eac": "eacRuntime",
}
# Switches Heroic has no field of its own for - kept in its env list.
_HEROIC_ENV_FLAGS = {"fsr": "WINE_FULLSCREEN_FSR", "proton_log": "PROTON_LOG", "vkbasalt": "ENABLE_VKBASALT"}


class HeroicBackend(LaunchOptionsBackend):
    launcher = "Heroic"

    @staticmethod
    def _app_name(game: GameItem) -> str:
        return game.uid.split(":", 2)[2]

    def _heroic_dir(self) -> Path:
        dirs = [base / "heroic" for base in HEROIC_CONFIG_ROOTS]
        for d in dirs:
            if (d / "GamesConfig").is_dir():
                return d
        for d in dirs:
            if d.is_dir():
                return d
        raise FileNotFoundError("Heroic's config folder wasn't found")

    def _config_path(self, game: GameItem) -> Path:
        name = self._app_name(game)
        for base in HEROIC_CONFIG_ROOTS:
            candidate = base / "heroic" / "GamesConfig" / f"{name}.json"
            if candidate.is_file():
                return candidate
        return self._heroic_dir() / "GamesConfig" / f"{name}.json"

    def _defaults(self) -> Dict:
        data = load_json(self._heroic_dir() / "config.json")
        defaults = data.get("defaultSettings") if isinstance(data, dict) else None
        return dict(defaults) if isinstance(defaults, dict) else {}

    def _settings(self, game: GameItem) -> Tuple[Dict, Dict]:
        """(the whole file, this game's settings merged over the defaults)."""
        name = self._app_name(game)
        doc = load_json(self._config_path(game))
        doc = doc if isinstance(doc, dict) else {}
        own = doc.get(name) if isinstance(doc.get(name), dict) else {}
        return doc, {**self._defaults(), **own}

    def where(self, game: GameItem) -> str:
        try:
            return str(self._config_path(game))
        except FileNotFoundError:
            return f"GamesConfig/{self._app_name(game)}.json"

    def load(self, game: GameItem) -> LaunchOptions:
        _doc, s = self._settings(game)
        opts = LaunchOptions()
        for attr, key in _HEROIC_FLAG_FIELDS.items():
            if key in s:
                setattr(opts, attr, bool(s[key]))
        if s.get("enableDXVKFpsLimit") and str(s.get("DXVKFpsCap") or "").strip():
            opts.fps_limit = str(s["DXVKFpsCap"]).strip()
        for entry in s.get("enviromentOptions") or []:
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            key, value = str(entry["key"]), str(entry.get("value", ""))
            flag = next((a for a, k in _HEROIC_ENV_FLAGS.items() if k == key), None)
            if flag and value == "1":
                setattr(opts, flag, True)
            else:
                opts.env[key] = value
        for entry in s.get("wrapperOptions") or []:
            if isinstance(entry, dict) and entry.get("exe"):
                opts.wrappers.append(str(entry["exe"]))
                opts.wrappers += _split(str(entry.get("args") or ""))
        opts.args = str(s.get("launcherArgs") or "")
        wine = s.get("wineVersion")
        if isinstance(wine, dict) and wine.get("bin"):
            opts.proton = str(wine["bin"])
        gs = s.get("gamescope") if isinstance(s.get("gamescope"), dict) else {}
        upscaling, limiter = bool(gs.get("enableUpscaling")), bool(gs.get("enableLimiter"))
        opts.gamescope = GamescopeOptions(
            enabled=upscaling or limiter,
            game_width=str(gs.get("gameWidth") or "") if upscaling else "",
            game_height=str(gs.get("gameHeight") or "") if upscaling else "",
            output_width=str(gs.get("upscaleWidth") or "") if upscaling else "",
            output_height=str(gs.get("upscaleHeight") or "") if upscaling else "",
            upscale_method=str(gs.get("upscaleMethod") or "") if upscaling else "",
            window_type=str(gs.get("windowType") or ""),
            fps_limit=str(gs.get("fpsLimiter") or "") if limiter else "",
            fps_limit_unfocused=str(gs.get("fpsLimiterNoFocus") or "") if limiter else "",
            force_grab_cursor=bool(gs.get("enableForceGrabCursor")),
            additional_options=str(gs.get("additionalOptions") or ""),
        )
        if opts.gamescope.upscale_method not in UPSCALE_METHODS:
            opts.gamescope.upscale_method = ""
        if opts.gamescope.window_type not in WINDOW_TYPES:
            opts.gamescope.window_type = ""
        return opts

    def save(self, game: GameItem, opts: LaunchOptions) -> None:
        from plunderhub.core.file_lock import held
        with held(self._config_path(game)):
            self._save(game, opts)

    def _save(self, game: GameItem, opts: LaunchOptions) -> None:
        name = self._app_name(game)
        path = self._config_path(game)
        doc, merged = self._settings(game)
        s = dict(merged)
        for attr, key in _HEROIC_FLAG_FIELDS.items():
            s[key] = bool(getattr(opts, attr))
        s["enableDXVKFpsLimit"] = bool(opts.fps_limit)
        if opts.fps_limit:
            s["DXVKFpsCap"] = opts.fps_limit
        env = [{"key": k, "value": "1"} for a, k in _HEROIC_ENV_FLAGS.items() if getattr(opts, a)]
        env += [{"key": k, "value": v} for k, v in opts.env.items()]
        s["enviromentOptions"] = env
        old_wrappers = [w for w in merged.get("wrapperOptions") or [] if isinstance(w, dict) and w.get("exe")]
        flat_old = [x for w in old_wrappers for x in [str(w["exe"]), *_split(str(w.get("args") or ""))]]
        if flat_old == opts.wrappers:
            s["wrapperOptions"] = old_wrappers      # unchanged: keep Heroic's own grouping
        else:
            s["wrapperOptions"] = ([{"exe": opts.wrappers[0], "args": shlex.join(opts.wrappers[1:])}]
                                   if opts.wrappers else [])
        s["launcherArgs"] = opts.args
        if opts.proton:
            s["wineVersion"] = self._wine_installation(opts.proton, merged.get("wineVersion"))
        gs = opts.gamescope
        has_size = any((gs.game_width, gs.game_height, gs.output_width, gs.output_height,
                        gs.upscale_method, gs.window_type))
        limiter = gs.enabled and bool(gs.fps_limit or gs.fps_limit_unfocused)
        # Heroic runs gamescope only when upscaling or the limiter is on.
        upscaling = gs.enabled and (has_size or not limiter)
        old_gs = merged.get("gamescope") if isinstance(merged.get("gamescope"), dict) else {}
        s["gamescope"] = {
            **old_gs,
            "enableUpscaling": upscaling,
            "enableLimiter": limiter,
            "enableForceGrabCursor": gs.force_grab_cursor,
            "windowType": gs.window_type,
            "gameWidth": gs.game_width,
            "gameHeight": gs.game_height,
            "upscaleWidth": gs.output_width,
            "upscaleHeight": gs.output_height,
            "upscaleMethod": gs.upscale_method,
            "fpsLimiter": gs.fps_limit,
            "fpsLimiterNoFocus": gs.fps_limit_unfocused,
            "additionalOptions": gs.additional_options,
        }
        out = dict(doc)
        out[name] = s
        out.setdefault("version", "v0")
        out["explicit"] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(out, indent=2, ensure_ascii=False)
        if path.exists():
            _write_with_backup(path, text)
        else:
            path.write_text(text, encoding="utf-8")

    def _wine_installations(self) -> List[Dict]:
        found: List[Dict] = []
        tools = self._heroic_dir() / "tools"
        for kind in ("proton", "wine"):
            try:
                entries = sorted((tools / kind).iterdir())
            except OSError:
                continue
            for entry in entries:
                binary = entry / "proton" if kind == "proton" else entry / "bin" / "wine"
                if binary.exists():
                    found.append({"bin": str(binary), "name": entry.name, "type": kind})
        for lib in sorted(steam_libraries(), key=str):
            try:
                entries = sorted((lib / "steamapps" / "common").iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name.lower().startswith("proton") and (entry / "proton").exists():
                    found.append({"bin": str(entry / "proton"), "name": f"Proton - {entry.name}", "type": "proton"})
        for root in steam_roots():
            try:
                entries = sorted((root / "compatibilitytools.d").iterdir())
            except OSError:
                continue
            for entry in entries:
                if (entry / "proton").exists():
                    found.append({"bin": str(entry / "proton"), "name": f"Proton - {entry.name}", "type": "proton"})
        return found

    def _wine_installation(self, bin_path: str, current) -> Dict:
        if isinstance(current, dict) and current.get("bin") == bin_path:
            return current
        for inst in self._wine_installations():
            if inst["bin"] == bin_path:
                return inst
        return {"bin": bin_path, "name": Path(bin_path).parent.name, "type": "proton"}

    def proton_choices(self, game: GameItem, current: Optional[str]) -> List[ProtonChoice]:
        unique: Dict[str, ProtonChoice] = {}
        try:
            installs = self._wine_installations()
        except FileNotFoundError:
            installs = []
        for inst in installs:
            unique.setdefault(inst["bin"], ProtonChoice(inst["bin"], inst["name"]))
        if current and current not in unique:
            _doc, s = self._settings(game)
            wine = s.get("wineVersion") if isinstance(s.get("wineVersion"), dict) else {}
            unique[current] = ProtonChoice(current, str(wine.get("name") or current))
        return list(unique.values())

    def _platform(self, game: GameItem) -> str:
        """The installed build's platform ("windows", "linux"...) from the
        store's installed.json."""
        from plunderhub.core.steam_heroic import heroic_roots
        _h, store_dir, game_id = game.uid.split(":", 2)
        for path in heroic_roots():
            if path.parent.name != store_dir:
                continue
            data = load_json(path)
            items = data.get("installed") if isinstance(data, dict) and isinstance(data.get("installed"), list) \
                else data if isinstance(data, list) else \
                [dict(v, appName=k) for k, v in data.items() if isinstance(v, dict)] if isinstance(data, dict) else []
            for item in items:
                if isinstance(item, dict) and game_id in (item.get("appName"), item.get("app_name"),
                                                          item.get("game_id"), item.get("id")):
                    return str(item.get("platform") or "").lower()
        return ""

    def prefix(self, game: GameItem) -> Optional[PrefixInfo]:
        _doc, s = self._settings(game)
        wine = s.get("wineVersion") if isinstance(s.get("wineVersion"), dict) else {}
        info = PrefixInfo("wine", runner=str(wine.get("name") or ""))
        if self._platform(game) in ("linux", "mac", "osx"):
            info.problem = "pfx_native"
            return info
        binary = str(wine.get("bin") or "")
        info.wine = proton_wine(Path(binary).parent) if wine.get("type") == "proton" else binary or None
        if not info.wine or not Path(info.wine).exists():
            info.problem = "pfx_no_runner"
        info.prefix = wine_prefix(s.get("winePrefix"))
        if info.prefix is None and not info.problem:
            info.problem = "pfx_missing"
        return info

    def advisory_process_running(self) -> bool:
        from plunderhub.core.heroic_launch import heroic_running
        return heroic_running()


# Shared helpers ---------------------------------------------------------------------

def _proton_dirs() -> List[Path]:
    """Where Proton builds are: Steam's compatibilitytools.d (ProtonPlus,
    umu and ProtonUp put theirs there), the system's, Steam's own."""
    return ([root / "compatibilitytools.d" for root in steam_roots()] + SYSTEM_COMPAT_TOOL_DIRS +
            [lib / "steamapps" / "common" for lib in steam_libraries()])


_WINE_KEYS = frozenset({"proton", "esync", "fsync", "wayland", "hdr", "fsr", "nvapi", "proton_log",
                        "battleye", "eac"})


def _dir_names(directory: Path, marker: Optional[str] = None) -> List[str]:
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    return [e.name for e in entries if e.is_dir() and (marker is None or (e / marker).exists())]


def _steam_proton_dir_names() -> List[str]:
    names: List[str] = []
    for lib in sorted(steam_libraries(), key=str):
        names += [n for n in _dir_names(lib / "steamapps" / "common", "proton") if n.lower().startswith("proton")]
    for directory in [root / "compatibilitytools.d" for root in steam_roots()] + SYSTEM_COMPAT_TOOL_DIRS:
        names += _dir_names(directory)
    return names


def _choices(names: List[str], current: Optional[str]) -> List[ProtonChoice]:
    unique: Dict[str, ProtonChoice] = {}
    for name in names:
        unique.setdefault(name, ProtonChoice(name, name))
    if current and current not in unique:
        unique[current] = ProtonChoice(current, current)
    return list(unique.values())


def _load_yaml(path: Path) -> Dict:
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    return data if isinstance(data, dict) else {}


def _dump_yaml(path: Path, data: Dict) -> None:
    import yaml
    text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    if path.exists():
        _write_with_backup(path, text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _flag_env_only(opts: LaunchOptions, native: frozenset) -> Dict[str, str]:
    """env_for(), minus the switches the launcher stores in fields of its own."""
    probe = replace(opts, env={})
    for attr in native:
        setattr(probe, attr, LaunchOptions().__dict__[attr])
    return {**env_for(probe), **opts.env}


def _absorb_flag_env(opts: LaunchOptions, env: Dict[str, str]) -> None:
    """Folds VAR=1 switches found in a launcher's env back into the model."""
    parsed = parse_command_line(" ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items()) + " %command%")
    for attr in ("esync", "fsync", "wayland", "hdr", "fsr", "nvapi", "proton_log", "mangohud", "vkbasalt"):
        if getattr(parsed, attr) != getattr(LaunchOptions(), attr):
            setattr(opts, attr, getattr(parsed, attr))
    if parsed.fps_limit:
        opts.fps_limit = parsed.fps_limit
    opts.env.update(parsed.env)


# Lutris -------------------------------------------------------------------------------

# wine: runner options and their Lutris defaults (lutris/runners/wine.py).
_LUTRIS_WINE_FIELDS = {
    "esync": ("esync", True),
    "fsync": ("fsync", True),
    "nvapi": ("dxvk_nvapi", True),
    "battleye": ("battleye", True),
    "eac": ("eac", True),
    "fsr": ("fsr", True),
    "hdr": ("proton_hdr", False),
}
_LUTRIS_WINDOW = {"-f": "fullscreen", "-b": "borderless"}
_GAMESCOPE_DEFAULT_SHARPNESS = "2"     # gamescope's own default


class LutrisBackend(LaunchOptionsBackend):
    launcher = "Lutris"

    def advisory_process_running(self) -> bool:
        return _process_running(("lutris",))

    @staticmethod
    def _game_id(game: GameItem) -> str:
        return game.uid.split(":", 1)[1]

    def _info(self, game: GameItem) -> Tuple[str, str]:
        from plunderhub.core.scanner import read_lutris_game_info
        info = read_lutris_game_info(self._game_id(game))
        if info is None:
            raise FileNotFoundError("the game isn't in Lutris's database")
        return info

    def _config_path(self, game: GameItem) -> Path:
        from plunderhub.core.scan_paths import lutris_game_config_path
        config_id, _runner = self._info(game)
        path = lutris_game_config_path(config_id)
        if path is None:
            raise FileNotFoundError(f"Lutris's {config_id}.yml wasn't found")
        return path

    def _is_wine(self, game: GameItem) -> bool:
        try:
            return self._info(game)[1] == "wine"
        except FileNotFoundError:
            return False

    def where(self, game: GameItem) -> str:
        try:
            return str(self._config_path(game))
        except FileNotFoundError:
            return "Lutris games/*.yml"

    def unsupported(self, game: GameItem) -> frozenset:
        return frozenset() if self._is_wine(game) else _WINE_KEYS

    def load(self, game: GameItem) -> LaunchOptions:
        data = _load_yaml(self._config_path(game))
        section = lambda name: data.get(name) if isinstance(data.get(name), dict) else {}
        game_s, system, wine = section("game"), section("system"), section("wine")
        opts = LaunchOptions()
        opts.args = str(game_s.get("args") or "")
        opts.gamemode = bool(system.get("gamemode", which("gamemoderun") is not None))
        opts.mangohud = bool(system.get("mangohud", False))
        env = system.get("env") if isinstance(system.get("env"), dict) else {}
        _absorb_flag_env(opts, {str(k): str(v) for k, v in env.items()})
        opts.wrappers = _split(str(system.get("prefix_command") or ""))
        if self._is_wine(game):
            for attr, (key, default) in _LUTRIS_WINE_FIELDS.items():
                setattr(opts, attr, bool(wine.get(key, default)))
            opts.proton = str(wine["version"]) if wine.get("version") else None
        gs = GamescopeOptions(enabled=bool(system.get("gamescope", False)))
        for res_key, (w_attr, h_attr) in (("gamescope_game_res", ("game_width", "game_height")),
                                          ("gamescope_output_res", ("output_width", "output_height"))):
            m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", str(system.get(res_key) or ""))
            if m:
                setattr(gs, w_attr, m.group(1))
                setattr(gs, h_attr, m.group(2))
        gs.window_type = _LUTRIS_WINDOW.get(str(system.get("gamescope_window_mode", "-f")), "")
        gs.fps_limit = str(system.get("gamescope_fps_limiter") or "")
        gs.force_grab_cursor = bool(system.get("gamescope_force_grab_cursor", False))
        if system.get("gamescope_fsr_sharpness") not in (None, ""):
            gs.upscale_method = "fsr"
        # Anything else (filters, unfocused fps, ...) lives in the free-form flags.
        flags = _split(str(system.get("gamescope_flags") or ""))
        if system.get("gamescope_hdr"):
            flags.append("--hdr-enabled")
        probe = LaunchOptions()
        _parse_gamescope(flags, probe)
        # Flags that have a field of their own move into it (unless Lutris's
        # own field already says otherwise); the rest stay free-form.
        for attr in ("game_width", "game_height", "output_width", "output_height", "upscale_method",
                     "window_type", "fps_limit", "fps_limit_unfocused", "force_grab_cursor"):
            if getattr(probe.gamescope, attr) and not getattr(gs, attr):
                setattr(gs, attr, getattr(probe.gamescope, attr))
        gs.additional_options = probe.gamescope.additional_options
        opts.gamescope = gs
        return opts

    def save(self, game: GameItem, opts: LaunchOptions) -> None:
        path = self._config_path(game)
        data = _load_yaml(path)
        for name in ("game", "system", "wine"):
            if not isinstance(data.get(name), dict):
                data[name] = {}
        game_s, system, wine = data["game"], data["system"], data["wine"]

        def put(section: Dict, key: str, value, default=None) -> None:
            if value in (None, "") or (default is not None and value == default and key not in section):
                section.pop(key, None)
            else:
                section[key] = value

        put(game_s, "args", opts.args)
        system["gamemode"] = opts.gamemode
        system["mangohud"] = opts.mangohud
        is_wine = self._is_wine(game)
        native = frozenset(_LUTRIS_WINE_FIELDS) if is_wine else frozenset()
        env = _flag_env_only(opts, native)
        put(system, "env", env or None)
        put(system, "prefix_command", shlex.join(opts.wrappers))
        if is_wine:
            for attr, (key, default) in _LUTRIS_WINE_FIELDS.items():
                wine[key] = bool(getattr(opts, attr))
            put(wine, "version", opts.proton)

        gs = opts.gamescope
        system["gamescope"] = gs.enabled
        put(system, "gamescope_game_res", f"{gs.game_width}x{gs.game_height}"
            if gs.game_width and gs.game_height else None)
        put(system, "gamescope_output_res", f"{gs.output_width}x{gs.output_height}"
            if gs.output_width and gs.output_height else None)
        reverse_window = {v: k for k, v in _LUTRIS_WINDOW.items()}
        if gs.window_type:
            system["gamescope_window_mode"] = reverse_window[gs.window_type]
        elif system.get("gamescope_window_mode") != "":   # keep an explicit "windowed"
            system.pop("gamescope_window_mode", None)
        put(system, "gamescope_fps_limiter", gs.fps_limit)
        system["gamescope_force_grab_cursor"] = gs.force_grab_cursor
        if gs.upscale_method == "fsr":
            system["gamescope_fsr_sharpness"] = str(system.get("gamescope_fsr_sharpness") or
                                                   _GAMESCOPE_DEFAULT_SHARPNESS)
        else:
            system.pop("gamescope_fsr_sharpness", None)
        extra = _split(gs.additional_options)
        system["gamescope_hdr"] = "--hdr-enabled" in extra
        extra = [w for w in extra if w != "--hdr-enabled"]
        if gs.upscale_method == "nis":
            extra = ["-F", "nis", *extra]
        elif gs.upscale_method in ("integer", "stretch"):
            extra = ["-S", gs.upscale_method, *extra]
        if gs.fps_limit_unfocused:
            extra = ["-o", gs.fps_limit_unfocused, *extra]
        put(system, "gamescope_flags", shlex.join(extra))
        if not wine:
            data.pop("wine")
        _dump_yaml(path, data)

    def prefix(self, game: GameItem) -> Optional[PrefixInfo]:
        from plunderhub.core.scan_paths import LUTRIS_CONFIG_DIR_CANDIDATES, LUTRIS_DATA_DIR_CANDIDATES
        info = PrefixInfo("wine")
        if not self._is_wine(game):
            info.problem = "pfx_native"
            return info
        data = _load_yaml(self._config_path(game))
        section = lambda d, name: d.get(name) if isinstance(d.get(name), dict) else {}
        version = str(section(data, "wine").get("version") or "")
        if not version:
            # the Wine runner's default (Preferences -> Runners -> Wine)
            for base in (*LUTRIS_CONFIG_DIR_CANDIDATES, *LUTRIS_DATA_DIR_CANDIDATES):
                version = str(section(_load_yaml(base / "runners" / "wine.yml"), "wine").get("version") or "")
                if version:
                    break
        runners = [base / "runners" / "wine" for base in LUTRIS_DATA_DIR_CANDIDATES]
        if not version:
            names = sorted((n for d in runners for n in _dir_names(d)), key=lambda n: n.lower())
            version = names[-1] if names else "system"
        info.runner = version
        if version.lower() == "system":
            info.wine = which("wine")
        else:
            own = next((d / version / "bin" / "wine" for d in runners if (d / version / "bin" / "wine").exists()),
                       None)
            proton = None if own else find_proton(version, _proton_dirs())
            info.wine = str(own) if own else proton_wine(proton) if proton else None
        if not info.wine:
            info.problem = "pfx_no_runner"
        info.prefix = wine_prefix(section(data, "game").get("prefix"))
        if info.prefix is None and not info.problem:
            info.problem = "pfx_missing"
        return info

    def proton_choices(self, game: GameItem, current: Optional[str]) -> List[ProtonChoice]:
        from plunderhub.core.scan_paths import LUTRIS_DATA_DIR_CANDIDATES
        names: List[str] = []
        for base in LUTRIS_DATA_DIR_CANDIDATES:
            names += _dir_names(base / "runners" / "wine")
        return _choices(names + _steam_proton_dir_names(), current)


# Faugus -------------------------------------------------------------------------------

# Faugus's runner values that aren't a Proton build to choose.
_FAUGUS_NON_PROTON_RUNNERS = ("Linux-Native", "Steam")


class FaugusBackend(LaunchOptionsBackend):
    """games.json has native gamemode/mangohud switches, the Proton as
    "runner", the game's arguments as "game_arguments", and
    "launch_arguments" - raw text Faugus places before umu-run - which
    carries everything else (VAR=value words, gamescope, other wrappers)."""
    launcher = "Faugus"
    anticheat_mode = None
    needs_rescan = True

    @staticmethod
    def _game_id(game: GameItem) -> str:
        return game.uid.split(":", 1)[1]

    def _path(self) -> Path:
        from plunderhub.core.scan_paths import faugus_games_path
        path = faugus_games_path()
        if path is None:
            raise FileNotFoundError("Faugus's games.json wasn't found")
        return path

    def _entries(self, game: GameItem) -> Tuple[List, Dict]:
        data = load_json(self._path())
        if not isinstance(data, list):
            raise ValueError("games.json isn't a list of games")
        for entry in data:
            if isinstance(entry, dict) and str(entry.get("gameid")) == self._game_id(game):
                return data, entry
        raise FileNotFoundError("the game isn't in Faugus's games.json")

    def where(self, game: GameItem) -> str:
        try:
            return str(self._path())
        except FileNotFoundError:
            return "games.json"

    # A store's launcher in Faugus (Battle.net, EA app...): game options don't
    # apply to it, and Faugus keeps Proton's Wayland driver off for most of them
    # (core/faugus_add.LAUNCHER_ENV) - greyed out, and kept so on every save.
    STORE_LAUNCHER_LOCKED = frozenset({"recommended", "wayland", "fps_check", "fps_entry", "win_fullscreen",
                                       "win_borderless", "launcher_skip", "fsr", "hdr"})

    def unsupported(self, game: GameItem) -> frozenset:
        base = super().unsupported(game)
        if game.store_platform in ("store-launcher", "launcher-game"):
            base = base | self.STORE_LAUNCHER_LOCKED
        try:
            _data, entry = self._entries(game)
        except (OSError, ValueError):
            return base | {"proton"}
        return base | {"proton"} if entry.get("runner") in _FAUGUS_NON_PROTON_RUNNERS else base

    def load(self, game: GameItem) -> LaunchOptions:
        _data, entry = self._entries(game)
        launch = str(entry.get("launch_arguments") or "")
        opts = parse_command_line(f"{launch} %command%") if launch.strip() else LaunchOptions()
        opts.args = str(entry.get("game_arguments") or "")
        opts.gamemode = opts.gamemode or bool(entry.get("gamemode"))
        opts.mangohud = opts.mangohud or bool(entry.get("mangohud"))
        runner = entry.get("runner")
        opts.proton = str(runner) if runner and runner not in _FAUGUS_NON_PROTON_RUNNERS else None
        return opts

    def save(self, game: GameItem, opts: LaunchOptions) -> None:
        from plunderhub.core.file_lock import held
        with held(self._path()):
            self._save(game, opts)

    def _save(self, game: GameItem, opts: LaunchOptions) -> None:
        data, entry = self._entries(game)
        if game.store_platform in ("store-launcher", "launcher-game"):
            from plunderhub.core.faugus_add import LAUNCHER_ENV, launcher_kind
            words = set(LAUNCHER_SKIP_ARG.split()) | {w for ws in WINDOW_ARGS.values() for w in ws}
            opts = replace(opts, wayland=False, fps_limit="", fsr=False, hdr=False,
                           args=" ".join(w for w in opts.args.split() if w not in words),
                           env={**{k: v for k, v in opts.env.items() if k != "DXVK_FRAME_RATE"},
                                **LAUNCHER_ENV.get(launcher_kind(str(entry.get("path") or "")) or "", {})})
        before = replace(opts, gamemode=False, args="")
        if opts.gamescope.enabled:
            # Faugus would put its own mangohud wrapper outside gamescope.
            entry["mangohud"] = ""
        else:
            before = replace(before, mangohud=False)
            entry["mangohud"] = True if opts.mangohud else ""
        launch = to_command_line(before)
        entry["launch_arguments"] = launch[: -len("%command%")].strip() if launch.endswith("%command%") else ""
        entry["game_arguments"] = opts.args
        entry["gamemode"] = True if opts.gamemode else ""
        if entry.get("runner") not in _FAUGUS_NON_PROTON_RUNNERS:
            entry["runner"] = opts.proton or ""
        path = self._path()
        _write_with_backup(path, json.dumps(data, indent=4, ensure_ascii=False))

    def proton_choices(self, game: GameItem, current: Optional[str]) -> List[ProtonChoice]:
        names = ["GE-Proton", "UMU-Proton"]
        for directory in [root / "compatibilitytools.d" for root in steam_roots()] + SYSTEM_COMPAT_TOOL_DIRS:
            names += _dir_names(directory)
        return _choices(names, current)

    def prefix(self, game: GameItem) -> Optional[PrefixInfo]:
        # Faugus runs its games through umu-run - so do its tools here
        from plunderhub.core.scan_paths import faugus_umu_path
        _data, entry = self._entries(game)
        runner = str(entry.get("runner") or "")
        info = PrefixInfo("umu", runner=runner or "UMU-Proton")
        if runner in _FAUGUS_NON_PROTON_RUNNERS:
            info.problem = "pfx_native"
            return info
        umu = faugus_umu_path()
        info.umu = str(umu) if umu else which("umu-run")
        if runner:
            info.env["PROTONPATH"] = runner
        if entry.get("protonfix"):
            info.env["GAMEID"] = str(entry["protonfix"])
        if not info.umu:
            info.problem = "pfx_no_runner"
        info.prefix = wine_prefix(entry.get("prefix"))
        if info.prefix is None and not info.problem:
            info.problem = "pfx_missing"
        return info

    def advisory_process_running(self) -> bool:
        return _process_running(("faugus-launcher", "faugus-launche"))


# Bottles ------------------------------------------------------------------------------

class BottlesBackend(LaunchOptionsBackend):
    """Program-level keys come from bottles/frontend/windows/launchoptions.py,
    the bottle-wide ones from BottleParams (bottles/backend/models/config.py).
    A Bottles program can only override its arguments and the gamescope
    and NVAPI switches; everything else is a setting of the whole bottle,
    shared by every program in it - the dialog says so."""
    launcher = "Bottles"
    anticheat_mode = None
    upscale_methods = ("", "integer")
    note_key = "lo_bottles_bottle_wide"

    @staticmethod
    def _program_id(game: GameItem) -> str:
        return game.uid.split(":", 1)[1]

    def _locate(self, game: GameItem) -> Tuple[Path, str]:
        from plunderhub.core.scan_paths import bottles_library_path
        library_path = bottles_library_path()
        if library_path is None:
            raise FileNotFoundError("Bottles's library.yml wasn't found")
        library = _load_yaml(library_path)
        bottles_root = library_path.parent / "bottles"
        custom = _load_yaml(library_path.parent / "data.yml").get("custom_bottles_path")
        if custom:
            bottles_root = Path(custom)
        for entry in library.values():
            if isinstance(entry, dict) and str(entry.get("id")) == self._program_id(game):
                bottle = entry.get("bottle") if isinstance(entry.get("bottle"), dict) else {}
                if bottle.get("path"):
                    return bottles_root / str(bottle["path"]) / "bottle.yml", self._program_id(game)
        raise FileNotFoundError("the program isn't in Bottles's library")

    def where(self, game: GameItem) -> str:
        try:
            return str(self._locate(game)[0])
        except FileNotFoundError:
            return "bottle.yml"

    def unsupported(self, game: GameItem) -> frozenset:
        return frozenset({"battleye", "eac", "gs_grab", "gs_additional", WRAPPER_KEY})

    def load(self, game: GameItem) -> LaunchOptions:
        path, program_id = self._locate(game)
        config = _load_yaml(path)
        params = config.get("Parameters") if isinstance(config.get("Parameters"), dict) else {}
        programs = config.get("External_Programs") if isinstance(config.get("External_Programs"), dict) else {}
        program = programs.get(program_id) if isinstance(programs.get(program_id), dict) else {}
        opts = LaunchOptions()
        opts.proton = str(config["Runner"]) if config.get("Runner") else None
        opts.gamemode = bool(params.get("gamemode", False))
        opts.mangohud = bool(params.get("mangohud", False))
        opts.vkbasalt = bool(params.get("vkbasalt", False))
        opts.fsr = bool(params.get("fsr", False))
        sync = str(params.get("sync", "wine"))
        opts.esync, opts.fsync = sync in ("esync", "fsync"), sync == "fsync"
        nvapi = program.get("dxvk_nvapi")
        opts.nvapi = bool(params.get("dxvk_nvapi", False) if nvapi is None else nvapi)
        env = config.get("Environment_Variables") if isinstance(config.get("Environment_Variables"), dict) else {}
        _absorb_flag_env(opts, {str(k): str(v) for k, v in env.items()})
        if program.get("arguments_enabled", True):
            opts.args = str(program.get("arguments") or "")
        enabled = program.get("gamescope")
        num = lambda key: str(params.get(key) or "") if params.get(key) not in (0, "0") else ""
        opts.gamescope = GamescopeOptions(
            enabled=bool(params.get("gamescope", False) if enabled is None else enabled),
            game_width=num("gamescope_game_width"), game_height=num("gamescope_game_height"),
            output_width=num("gamescope_window_width"), output_height=num("gamescope_window_height"),
            upscale_method="integer" if params.get("gamescope_scaling") else "",
            window_type=("fullscreen" if params.get("gamescope_fullscreen", True)
                         else "borderless" if params.get("gamescope_borderless") else ""),
            fps_limit=num("gamescope_fps"), fps_limit_unfocused=num("gamescope_fps_no_focus"),
        )
        return opts

    def save(self, game: GameItem, opts: LaunchOptions) -> None:
        if opts.wrappers:
            raise ValueError("Bottles can't store wrapper commands: " + shlex.join(opts.wrappers))
        path, program_id = self._locate(game)
        config = _load_yaml(path)
        params = config.setdefault("Parameters", {})
        programs = config.setdefault("External_Programs", {})
        program = programs.get(program_id)
        if not isinstance(program, dict):
            raise FileNotFoundError("the program isn't in its bottle.yml")
        if opts.proton:
            config["Runner"] = opts.proton
        params["gamemode"] = opts.gamemode
        params["mangohud"] = opts.mangohud
        params["vkbasalt"] = opts.vkbasalt
        params["fsr"] = opts.fsr
        old_sync = str(params.get("sync", "wine"))
        if (opts.esync, opts.fsync) != (old_sync in ("esync", "fsync"), old_sync == "fsync"):
            # Only when the switches changed: they can't express e.g. "futex2".
            params["sync"] = "fsync" if opts.fsync else "esync" if opts.esync else "wine"
        program["dxvk_nvapi"] = opts.nvapi
        native = frozenset({"esync", "fsync", "fsr", "nvapi", "vkbasalt"})
        config["Environment_Variables"] = _flag_env_only(opts, native)
        program["arguments"] = opts.args
        program["arguments_enabled"] = bool(opts.args)
        gs = opts.gamescope
        program["gamescope"] = gs.enabled
        to_int = lambda v: int(v) if str(v).isdigit() else 0
        params.update({
            "gamescope_game_width": to_int(gs.game_width), "gamescope_game_height": to_int(gs.game_height),
            "gamescope_window_width": to_int(gs.output_width), "gamescope_window_height": to_int(gs.output_height),
            "gamescope_fps": to_int(gs.fps_limit), "gamescope_fps_no_focus": to_int(gs.fps_limit_unfocused),
            "gamescope_scaling": gs.upscale_method == "integer",
            # Bottles has just these two switches: neither on is windowed.
            "gamescope_fullscreen": gs.window_type == "fullscreen",
            "gamescope_borderless": gs.window_type == "borderless",
        })
        _dump_yaml(path, config)

    def prefix(self, game: GameItem) -> Optional[PrefixInfo]:
        # the bottle is the prefix; its runner is one of Bottles's own
        path, _program_id = self._locate(game)
        runner = str(_load_yaml(path).get("Runner") or "")
        info = PrefixInfo("wine", runner=runner)
        if runner.startswith("sys-"):
            info.wine = which("wine")
        else:
            from plunderhub.core.scan_paths import bottles_library_path
            folder = bottles_library_path().parent / "runners" / runner
            info.wine = str(folder / "bin" / "wine") if (folder / "bin" / "wine").exists() else proton_wine(folder)
        if not runner or not info.wine:
            info.problem = "pfx_no_runner"
        info.prefix = wine_prefix(path.parent)
        if info.prefix is None and not info.problem:
            info.problem = "pfx_missing"
        return info

    def proton_choices(self, game: GameItem, current: Optional[str]) -> List[ProtonChoice]:
        from plunderhub.core.scan_paths import BOTTLES_DATA_DIR_CANDIDATES
        names: List[str] = []
        for base in BOTTLES_DATA_DIR_CANDIDATES:
            names += _dir_names(base / "runners")
        return _choices(names, current)

    def advisory_process_running(self) -> bool:
        return _process_running(("bottles",))


def backend_for(game: GameItem) -> Optional[LaunchOptionsBackend]:
    if game.uid.startswith("steam:"):
        return SteamBackend()
    if game.launcher == "Heroic" and game.uid.count(":") >= 2:
        return HeroicBackend()
    if game.uid.startswith("lutris:"):
        return LutrisBackend()
    if game.uid.startswith("faugus:"):
        return FaugusBackend()
    if game.uid.startswith("bottles:"):
        return BottlesBackend()
    return None
