# Maintainer: Ikraja
pkgname=plunderhub
pkgver=1.0.0
pkgrel=1
# The upstream git tag. pacman versions can't contain "-" and the tag
# does, so the two are spelled out separately rather than translated
# back and forth.
_tag="v1.0.0"
pkgdesc="PlunderHub (Beta) - Unified game launcher"
arch=('any')
url="https://github.com/IkrajaDev/PlunderHub"
license=('GPL-3.0-or-later')
# libwebp: its demux library plays long animated banners frame by frame;
# librsvg: the SVG icons and launcher logos (gdk-pixbuf's glycin loaders
# bring WebP and SVG on current Arch; webp-pixbuf-loader does it where
# gdk-pixbuf is older, as on frozen Arch-based systems);
# libayatana-appindicator: the tray icon, on by default;
# polkit: pkexec, which Shipwright installs tools and launchers through
depends=('python' 'python-gobject' 'python-cairo' 'gtk3' 'python-yaml' 'libsecret' 'libwebp' 'librsvg'
         'libayatana-appindicator' 'polkit')

optdepends=(
    'steam: Steam games support'
    'heroic-games-launcher-bin: Heroic games support (AUR)'
    'lutris: Lutris games support'
    'bottles: Bottles games support (AUR)'
    'faugus-launcher: Faugus games support (AUR)'
    'xdg-utils: launching Bottles games'
    'gamemode: GameMode launch option'
    'mangohud: MangoHud launch option'
    'gamescope: gamescope launch option'
    'flatpak: installing launchers from Flathub'
    "pciutils: the graphics card's name in Ship inspection"
    'webp-pixbuf-loader: WebP covers and banners where gdk-pixbuf has no glycin loaders'
)
options=('!strip' '!debug')

# GitHub names extracted archives after the repo and the tag without a
# leading "v"; tools/make-local-tarball.sh produces the same layout.
_srcdir_name="PlunderHub-${_tag#v}"

# A tarball, never individual local files reached for via $startdir at
# INSTALL time. $startdir is empty under `makepkg --clean`, in a chroot
# and on the AUR, so a package built there came out missing the entire
# application.
#
# Which tarball depends on where this PKGBUILD is sitting:
#
#   - In a full source checkout (the usual case while developing, and
#     the only one that works before a version is tagged on GitHub):
#     the tree next to this file is packed into a tarball and used.
#     Regenerated whenever the source is newer, so an edit can't be
#     silently packaged from a stale archive.
#
#   - On its own, as on the AUR: the published release is downloaded.
#
# The tarball is built here rather than left to a separate command
# because forgetting that command produced a 404 on the release URL and
# nothing pointing at the real cause.
_local_tarball="$startdir/$pkgname-$pkgver.tar.gz"

if [[ -f "$startdir/tools/make-local-tarball.sh" && -f "$startdir/plunderhub/constants.py" ]]; then
    if [[ ! -f "$_local_tarball" || "$startdir/plunderhub/constants.py" -nt "$_local_tarball" ]]; then
        echo "==> Packing the local source tree into $pkgname-$pkgver.tar.gz"
        # Failing loudly here matters: falling through to the download
        # would report a 404 on a URL the user never asked for.
        bash "$startdir/tools/make-local-tarball.sh" >/dev/null || {
            echo "==> ERROR: tools/make-local-tarball.sh failed - see the message above." >&2
            return 1
        }
    fi
fi

if [[ -f "$_local_tarball" ]]; then
    source=("$pkgname-$pkgver.tar.gz")
else
    source=("$pkgname-$pkgver.tar.gz::$url/archive/refs/tags/$_tag.tar.gz")
fi
sha256sums=('SKIP')

package() {
    cd "$srcdir/$_srcdir_name"

    # Thin launcher -> /usr/bin/plunderhub. The application itself goes
    # to /usr/lib/plunderhub/ below; plunderhub.py finds it there via
    # the sys.path candidates at the top of the file.
    install -Dm755 plunderhub.py "$pkgdir/usr/bin/plunderhub"

    # The plunderhub/ package: code plus bundled assets. The file types
    # cover the Lucide icon set (assets/icons), the Inter font
    # (assets/fonts) and their LICENSE files - without them the app runs
    # but falls back to the system icon theme and font. Keep in sync
    # with what those folders actually contain.
    find plunderhub -type f \
        \( -name "*.py" -o -name "*.png" -o -name "*.svg" \
           -o -name "*.ttf" -o -name "*.txt" \) -print0 |
    while IFS= read -r -d '' pyfile; do
        install -Dm644 "$pyfile" "$pkgdir/usr/lib/plunderhub/$pyfile"
    done

    install -Dm644 PlunderHub.desktop \
        "$pkgdir/usr/share/applications/io.github.ikraja.PlunderHub.desktop"

    # App icon (window/taskbar/tray/app menu). Separate from the copy
    # under /usr/lib - this is the one that registers with the hicolor
    # theme. See _app_icon_name() in plunderhub/theme.py.
    install -Dm644 plunderhub/assets/PlunderHub_ikon.svg \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/PlunderHub.svg"

    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
