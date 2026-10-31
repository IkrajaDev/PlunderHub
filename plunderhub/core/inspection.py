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

"""Ship inspection: the system settings games trip over most - 32-bit
packages switched off, a missing (32-bit) Vulkan driver, a low
vm.max_map_count or file limit, ntsync not loaded. Each check says
what it found and, where it can, how Shipwright fixes it (see
ui/launcher_tasks.run_fix and core/root_helper's actions)."""

import glob
import os
import resource
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from plunderhub.core.utils import detect_linux_distro_family, immutable_system

OK, WARN, INFO = "ok", "warn", "info"

MAP_COUNT_MIN = 1048576     # what most distributions (and Proton's docs) settle on
NOFILE_MIN = 524288         # esync needs many file descriptors


@dataclass
class Fix:
    actions: Tuple[str, ...] = ()               # root_helper actions
    commands: Tuple[Tuple[str, ...], ...] = ()  # package-manager commands
    packages: Tuple[str, ...] = ()              # to install
    done_key: str = "insp_fix_done"


@dataclass
class Check:
    id: str
    status: str
    title_key: str
    text_key: str
    args: Dict[str, str] = field(default_factory=dict)
    fix: Optional[Fix] = None
    blocked_key: Optional[str] = None           # why the fix can't run yet


def _run(cmd: List[str]) -> Tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env={**os.environ, "LC_ALL": "C"})
        return out.returncode, out.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


# --- 32-bit packages ---------------------------------------------------------------

def _multilib_on(family: str) -> Optional[bool]:
    if family == "arch":
        code, out = _run(["pacman-conf", "--repo-list"])
        return "multilib" in out.split() if code == 0 else None
    if family == "debian":
        return "i386" in _run(["dpkg", "--print-foreign-architectures"])[1].split()
    return None     # Fedora and openSUSE always carry their 32-bit packages


def _check_multilib(family: str, on: Optional[bool]) -> Optional[Check]:
    if on is None:
        return None
    key = "multilib" if family == "arch" else "i386"
    if on:
        return Check("multilib", OK, f"insp_{key}_title", f"insp_{key}_ok")
    if family == "arch":
        fix = Fix(actions=("enable-multilib",), commands=(("pacman", "-Syu", "--noconfirm"),),
                  done_key="insp_multilib_done")
    else:
        fix = Fix(commands=(("dpkg", "--add-architecture", "i386"), ("apt-get", "update")), done_key="insp_i386_done")
    return Check("multilib", WARN, f"insp_{key}_title", f"insp_{key}_off", fix=fix)


# --- graphics cards and their Vulkan drivers ----------------------------------------

@dataclass
class Gpu:
    vendor: str         # amd | intel | nvidia
    driver: str         # the kernel driver bound to it: amdgpu, radeon, i915, xe, nvidia, nouveau...
    name: str           # "AMD Radeon RX 9070 XT", or just the vendor's name


_VENDORS = {"0x1002": "amd", "0x8086": "intel", "0x10de": "nvidia"}
_VENDOR_NAMES = {"amd": "AMD", "intel": "Intel", "nvidia": "NVIDIA"}
# family -> (64-bit dirs, 32-bit dirs)
_LIB_DIRS = {
    "arch": (("/usr/lib",), ("/usr/lib32",)),
    "debian": (("/usr/lib/x86_64-linux-gnu",), ("/usr/lib/i386-linux-gnu",)),
    "fedora": (("/usr/lib64",), ("/usr/lib",)),
    "suse": (("/usr/lib64",), ("/usr/lib",)),
}
# Mesa's Vulkan drivers, by kernel driver: the library any of which shows
# it's there, and per family the (64-bit, 32-bit) packages.
_MESA = {
    "amdgpu": (("libvulkan_radeon.so",), {
        "arch": ("vulkan-radeon", "lib32-vulkan-radeon"),
        "debian": ("mesa-vulkan-drivers", "mesa-vulkan-drivers:i386"),
        "fedora": ("mesa-vulkan-drivers", "mesa-vulkan-drivers.i686"),
        "suse": ("libvulkan_radeon", "libvulkan_radeon-32bit")}),
    "intel": (("libvulkan_intel.so", "libvulkan_intel_hasvk.so"), {
        "arch": ("vulkan-intel", "lib32-vulkan-intel"),
        "debian": ("mesa-vulkan-drivers", "mesa-vulkan-drivers:i386"),
        "fedora": ("mesa-vulkan-drivers", "mesa-vulkan-drivers.i686"),
        "suse": ("libvulkan_intel", "libvulkan_intel-32bit")}),
    "nouveau": (("libvulkan_nouveau.so",), {      # NVK
        "arch": ("vulkan-nouveau", "lib32-vulkan-nouveau"),
        "debian": ("mesa-vulkan-drivers", "mesa-vulkan-drivers:i386"),
        "fedora": ("mesa-vulkan-drivers", "mesa-vulkan-drivers.i686"),
        "suse": ("libvulkan_nouveau", "libvulkan_nouveau-32bit")}),
}
_NVIDIA_LIB = "libGLX_nvidia.so.0"


def _gpu_name(slot: str, vendor: str) -> str:
    """lspci's name for the card, shortened: "Navi 48 [Radeon RX 9070 XT]"
    becomes "AMD Radeon RX 9070 XT"."""
    code, out = _run(["lspci", "-vmm", "-s", slot])
    device = next((line.split(":", 1)[1].strip() for line in out.splitlines()
                   if line.startswith("Device:")), "") if code == 0 else ""
    if "[" in device and device.endswith("]"):
        device = device[device.rindex("[") + 1:-1]
    if device.startswith("Device "):
        device = ""     # an older pci.ids that doesn't know the card yet
    return f"{_VENDOR_NAMES[vendor]} {device}".strip() if device else _VENDOR_NAMES[vendor]


def gpus() -> List[Gpu]:
    """Every graphics card in the computer (a laptop's integrated one and
    its discrete one both), with the kernel driver it runs on."""
    found: List[Gpu] = []
    seen = set()
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if not os.path.basename(card)[4:].isdigit():
            continue        # card1-DP-1 and the like are its connectors
        device = os.path.realpath(os.path.join(card, "device"))
        vendor = _VENDORS.get(_read(os.path.join(device, "vendor")).lower())
        if vendor is None or device in seen:
            continue
        seen.add(device)
        driver_link = os.path.join(device, "driver")
        driver = os.path.basename(os.path.realpath(driver_link)) if os.path.exists(driver_link) else ""
        found.append(Gpu(vendor, driver, _gpu_name(os.path.basename(device), vendor)))
    return found


def _has_lib(dirs, libs) -> Optional[str]:
    for d in dirs:
        for lib in libs:
            path = os.path.join(d, lib)
            if os.path.exists(path):
                return path
    return None


def _owner(family: str, path: str) -> Optional[str]:
    if family == "arch":
        code, out = _run(["pacman", "-Qqo", path])
    elif family in ("fedora", "suse"):
        code, out = _run(["rpm", "-qf", "--qf", "%{NAME}\n", path])
    else:
        return None
    return out.split()[0] if code == 0 and out.strip() else None


def _check_gpu(family: str, gpu: Gpu, multilib: Optional[bool]) -> List[Check]:
    dirs = _LIB_DIRS.get(family)
    args = {"gpu": gpu.name}
    if dirs is None or not gpu.driver:
        return []
    if gpu.driver == "radeon":
        # pre-GCN, or GCN 1/2 left on the old driver: no Vulkan at all
        return [Check(f"gpu-{gpu.name}", WARN, "insp_vulkan_title", "insp_gpu_radeon_old", args)]
    if gpu.driver == "nvidia":
        lib64 = _has_lib(dirs[0], (_NVIDIA_LIB,))
        if lib64 is None:
            return [Check(f"gpu-{gpu.name}", WARN, "insp_vulkan_title", "insp_vulkan64_missing_nvidia", args)]
        if _has_lib(dirs[1], (_NVIDIA_LIB,)):
            return [Check(f"gpu-{gpu.name}", OK, "insp_vulkan_title", "insp_vulkan_ok", args)]
        # the 32-bit half of whichever driver branch is installed
        # (nvidia-utils, nvidia-580xx-utils...)
        owner = _owner(family, lib64)
        package = {"arch": f"lib32-{owner}" if owner else None,
                   "fedora": f"{owner}.i686" if owner else None}.get(family)
        fix = Fix(packages=(package,), done_key="insp_vulkan_done") if package else None
        blocked = "insp_needs_multilib" if fix and multilib is False else None
        return [Check(f"gpu-{gpu.name}", WARN, "insp_vulkan_title", "insp_vulkan32_missing", args, fix, blocked)]
    kind = "intel" if gpu.driver in ("i915", "xe") else gpu.driver
    if kind not in _MESA:
        return []
    libs, packages = _MESA[kind]
    pkgs = packages.get(family, (None, None))
    checks = []
    if gpu.driver == "nouveau":
        checks.append(Check(f"nouveau-{gpu.name}", INFO, "insp_gpu_title", "insp_gpu_nouveau", args))
    if not _has_lib(dirs[0], libs):
        # the 32-bit half along with it, where 32-bit packages can come
        both = (pkgs[0],) + ((pkgs[1],) if pkgs[1] and multilib is not False else ())
        fix = Fix(packages=both, done_key="insp_vulkan_done") if pkgs[0] else None
        checks.append(Check(f"gpu-{gpu.name}", WARN, "insp_vulkan_title", "insp_vulkan64_missing", args, fix))
    elif not _has_lib(dirs[1], libs):
        fix = Fix(packages=(pkgs[1],), done_key="insp_vulkan_done") if pkgs[1] else None
        blocked = "insp_needs_multilib" if fix and multilib is False else None
        checks.append(Check(f"gpu-{gpu.name}", WARN, "insp_vulkan_title", "insp_vulkan32_missing", args, fix,
                            blocked))
    else:
        checks.append(Check(f"gpu-{gpu.name}", OK, "insp_vulkan_title", "insp_vulkan_ok", args))
    return checks


def _repo_has(family: str, package: str) -> Optional[bool]:
    if family == "arch":
        return _run(["pacman", "-Si", package])[0] == 0
    if family == "debian":
        code, out = _run(["apt-cache", "policy", package])
        return code == 0 and "Candidate:" in out and "Candidate: (none)" not in out
    return None     # dnf / zypper can't be asked quickly - the install finds out


def _first_available(family: str, candidates: Tuple[str, ...]) -> Optional[str]:
    for package in candidates:
        if all(_repo_has(family, p) is not False for p in package.split()):
            return package
    return None


# The 32-bit Vulkan loader: without it Steam and 32-bit games see no
# Vulkan even with the driver there.
_LOADER32 = {"arch": "lib32-vulkan-icd-loader", "debian": "libvulkan1:i386",
             "fedora": "vulkan-loader.i686", "suse": "libvulkan1-32bit"}


def _check_vulkan_loader(family: str, multilib: Optional[bool]) -> Optional[Check]:
    dirs = _LIB_DIRS.get(family)
    if dirs is None:
        return None
    if _has_lib(dirs[1], ("libvulkan.so.1",)):
        return Check("loader32", OK, "insp_loader_title", "insp_loader_ok")
    fix = Fix(packages=(_LOADER32[family],), done_key="insp_loader_done")
    return Check("loader32", WARN, "insp_loader_title", "insp_loader_missing", fix=fix,
                 blocked_key="insp_needs_multilib" if multilib is False else None)


# Fonts Windows games and Steam expect: Liberation stands in for Arial,
# Times New Roman and Courier New; a CJK font keeps Chinese, Japanese and
# Korean text from turning into boxes.
_FONTS = {
    "liberation": ("Liberation Sans", {"arch": "ttf-liberation", "debian": "fonts-liberation",
                                       "fedora": "liberation-fonts-all", "suse": "liberation-fonts"}),
    "cjk": (None, {"arch": "wqy-zenhei", "debian": "fonts-wqy-zenhei",
                   "fedora": "wqy-zenhei-fonts", "suse": "wqy-zenhei-fonts"}),
}


def _check_fonts(family: str) -> List[Check]:
    code, families = _run(["fc-list", ":", "family"])
    if code != 0:
        return []
    checks = []
    for key, (name, packages) in _FONTS.items():
        if name is not None:
            have = name.lower() in families.lower()
        else:
            have = bool(_run(["fc-list", ":lang=zh", "family"])[1].strip())
        if have:
            checks.append(Check(f"font-{key}", OK, f"insp_font_{key}_title", f"insp_font_{key}_ok"))
        else:
            package = packages.get(family)
            fix = Fix(packages=(package,), done_key="insp_font_done") if package else None
            checks.append(Check(f"font-{key}", WARN, f"insp_font_{key}_title", f"insp_font_{key}_missing", fix=fix))
    return checks


# Hardware video decoding (VA-API): cutscenes, browsers, OBS. The driver
# file, and the packages that may bring it, best first (a candidate may
# be several packages, space-separated).
_VA_DIRS = {"arch": "/usr/lib/dri", "debian": "/usr/lib/x86_64-linux-gnu/dri",
            "fedora": "/usr/lib64/dri", "suse": "/usr/lib64/dri"}
_VA = {
    "amdgpu": (("radeonsi_drv_video.so",), {"arch": ("mesa",), "debian": ("mesa-va-drivers", "mesa-libgallium"),
                                             "fedora": ("mesa-va-drivers",), "suse": ("Mesa-libva Mesa-dri",)}),
    "nouveau": (("nouveau_drv_video.so",), {"arch": ("mesa",), "debian": ("mesa-va-drivers", "mesa-libgallium"),
                                             "fedora": ("mesa-va-drivers",), "suse": ("Mesa-libva Mesa-dri",)}),
    "intel": (("iHD_drv_video.so", "i965_drv_video.so"),
              {"arch": ("intel-media-driver",), "debian": ("intel-media-va-driver-non-free", "intel-media-va-driver"),
               "fedora": ("libva-intel-media-driver",), "suse": ("intel-media-driver",)}),
    "nvidia": (("nvidia_drv_video.so",), {"arch": ("libva-nvidia-driver",), "debian": ("nvidia-vaapi-driver",),
                                          "fedora": ("libva-nvidia-driver",)}),
}


def _check_vaapi(family: str, gpu: Gpu) -> Optional[Check]:
    directory = _VA_DIRS.get(family)
    kind = "intel" if gpu.driver in ("i915", "xe") else gpu.driver
    if directory is None or kind not in _VA:
        return None
    files, packages = _VA[kind]
    args = {"gpu": gpu.name}
    if any(os.path.exists(os.path.join(directory, f)) for f in files):
        return Check(f"va-{gpu.name}", OK, "insp_va_title", "insp_va_ok", args)
    package = _first_available(family, packages.get(family, ()))
    fix = Fix(packages=tuple(package.split()), done_key="insp_va_done") if package else None
    # nice to have, not something games fail without
    return Check(f"va-{gpu.name}", INFO, "insp_va_title", "insp_va_missing", args, fix)


# --- kernel settings ---------------------------------------------------------------

def _check_map_count() -> Optional[Check]:
    value = _read("/proc/sys/vm/max_map_count")
    if not value.isdigit():
        return None
    args = {"value": value}
    if int(value) >= MAP_COUNT_MIN:
        return Check("map_count", OK, "insp_map_title", "insp_map_ok", args)
    return Check("map_count", WARN, "insp_map_title", "insp_map_low", args,
                 Fix(actions=("max-map-count",), done_key="insp_map_done"))


def _check_nofile() -> Check:
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    args = {"value": "∞" if hard == resource.RLIM_INFINITY else str(hard)}
    if hard == resource.RLIM_INFINITY or hard >= NOFILE_MIN:
        return Check("nofile", OK, "insp_nofile_title", "insp_nofile_ok", args)
    # the fix writes systemd's own limit files: nothing else reads them
    fix = Fix(actions=("nofile-limit",), done_key="insp_nofile_done") \
        if os.path.isdir("/run/systemd/system") else None
    return Check("nofile", WARN, "insp_nofile_title", "insp_nofile_low", args, fix)


def _kernel_has_ntsync() -> bool:
    """Its module (or built in) - asked of modinfo, else the module
    files themselves, else the kernel version (6.14 brought it)."""
    if _run(["modinfo", "-n", "ntsync"])[0] == 0:
        return True
    release = os.uname().release
    moddir = f"/lib/modules/{release}"
    if os.path.isdir(moddir):
        if glob.glob(f"{moddir}/kernel/drivers/misc/ntsync.ko*"):
            return True
        return "ntsync" in _read(f"{moddir}/modules.builtin")
    try:
        major, minor = (int(x) for x in release.split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= (6, 14)


def _check_ntsync() -> Check:
    if os.path.exists("/dev/ntsync"):
        return Check("ntsync", OK, "insp_ntsync_title", "insp_ntsync_ok")
    if _kernel_has_ntsync():
        return Check("ntsync", WARN, "insp_ntsync_title", "insp_ntsync_off",
                     fix=Fix(actions=("load-ntsync",), done_key="insp_ntsync_done"))
    return Check("ntsync", INFO, "insp_ntsync_title", "insp_ntsync_none", {"kernel": os.uname().release})


def run_checks() -> List[Check]:
    """Every check, in order. Takes a moment (pacman-conf, modinfo) -
    call it off the GTK thread."""
    family = detect_linux_distro_family()
    multilib = _multilib_on(family)
    checks = [_check_multilib(family, multilib)]
    cards = gpus()
    for gpu in cards:
        checks += _check_gpu(family, gpu, multilib)
    checks.append(_check_vulkan_loader(family, multilib))
    checks += [_check_vaapi(family, gpu) for gpu in cards]
    checks += _check_fonts(family)
    checks += [_check_map_count(), _check_nofile(), _check_ntsync()]
    checks = [c for c in checks if c is not None]
    if immutable_system():
        # packages can't be installed from here; /etc fixes still work
        for c in checks:
            if c.fix is not None and (c.fix.packages or c.fix.commands):
                c.blocked_key = "insp_immutable"
    return checks
