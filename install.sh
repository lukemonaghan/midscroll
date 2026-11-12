#!/usr/bin/env bash
# Manual install for any systemd distro. Prefer the packages in packaging/
# if one exists for your distro. Dependencies are not installed here; see
# README.md.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$EUID" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

install -Dm755 midscroll.py /usr/bin/midscroll
install -Dm755 midscroll-overlay.py /usr/bin/midscroll-overlay
install -Dm755 midscroll-settings.py /usr/bin/midscroll-settings
install -Dm755 midscroll-apply.py /usr/bin/midscroll-apply
install -Dm644 io.github.gnhen.midscroll.Settings.desktop \
    /usr/share/applications/io.github.gnhen.midscroll.Settings.desktop
install -Dm644 io.github.gnhen.midscroll.policy \
    /usr/share/polkit-1/actions/io.github.gnhen.midscroll.policy
install -Dm644 systemd/midscroll.service /etc/systemd/system/midscroll.service
install -Dm644 systemd/midscroll-overlay.service \
    /usr/lib/systemd/user/midscroll-overlay.service
install -Dm644 icons/move-vertical.svg /usr/share/midscroll/move-vertical.svg
install -Dm644 icons/move-vertical.svg \
    /usr/share/icons/hicolor/scalable/apps/midscroll.svg
# GNOME shell extensions: focus reporting (app blacklist) and the autoscroll
# cursor. Installed system-wide; each user enables them (see the note below).
for ext in midscroll-focus@lukem midscroll-indicator@lukem; do
    install -Dm644 "gnome-extensions/$ext/extension.js" \
        "/usr/share/gnome-shell/extensions/$ext/extension.js"
    install -Dm644 "gnome-extensions/$ext/metadata.json" \
        "/usr/share/gnome-shell/extensions/$ext/metadata.json"
done
# Don't clobber an existing tuned config
[ -f /etc/midscroll.conf ] || install -Dm644 midscroll.conf /etc/midscroll.conf

systemctl daemon-reload
systemctl enable --now midscroll.service
systemctl --global enable midscroll-overlay.service

# Start the overlay in the invoking user's running session too
if [ -n "${SUDO_USER:-}" ]; then
    uid=$(id -u "$SUDO_USER")
    sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$uid" \
        systemctl --user daemon-reload || :
    sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$uid" \
        systemctl --user start midscroll-overlay.service || :
fi

echo
echo "midscroll is installed, running, and enabled on startup."
echo "Tune the feel in /etc/midscroll.conf, then: sudo systemctl restart midscroll"
echo
echo "On GNOME, also run (as your user), then log out and back in:"
echo "  gnome-extensions enable midscroll-focus@lukem midscroll-indicator@lukem"
