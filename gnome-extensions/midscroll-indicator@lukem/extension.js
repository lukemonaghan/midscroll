// midscroll indicator - the autoscroll cursor for GNOME.
//
// midscroll-overlay draws its badge with wlr-layer-shell, which GNOME's
// compositor doesn't implement, so on GNOME nothing showed that autoscroll
// was running. Code inside the shell can draw over everything, so this
// extension does it: while a drag-scroll is active it puts an origin marker
// where the pointer was pinned, with an arrow per direction that lights up
// (and grows) the further you drag that way, and hides the real pointer so
// the marker *is* the cursor.
//
// midscroll-overlay drives it over D-Bus: SetState(active, dx, dy), where
// dx/dy is the drag offset from the anchor. It is purely visual - the
// extension never sees or sends input.
//
// Safety: the pointer is only hidden while state keeps arriving. The overlay
// re-sends every second while a drag is active; if that stops for
// WATCHDOG_MS (overlay crashed, daemon gone) the marker is removed and the
// pointer comes back by itself. disable() always restores it too.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE_XML = `
<node>
  <interface name="org.gnome.Shell.Extensions.MidscrollIndicator">
    <method name="SetState">
      <arg type="b" direction="in" name="active"/>
      <arg type="i" direction="in" name="dx"/>
      <arg type="i" direction="in" name="dy"/>
    </method>
  </interface>
</node>`;

const SIZE = 120;          // square actor; the marker is drawn centred in it
const RADIUS = 26;         // marker disc
const DEAD = 12;           // px of drag before a direction starts to light
const FULL = 160;          // px of drag for a fully lit, fully grown arrow
const WATCHDOG_MS = 3000;  // no state for this long => take everything down
const HIDE_POINTER = true; // the marker replaces the pointer while active

export default class MidscrollIndicatorExtension extends Extension {
    enable() {
        this._active = false;
        this._dx = 0;
        this._dy = 0;
        this._lastCall = 0;
        this._watchdog = 0;
        this._pointerHidden = false;

        this._area = new St.DrawingArea({
            width: SIZE, height: SIZE, reactive: false, visible: false,
        });
        this._area.connect('repaint', area => this._paint(area));
        // Top chrome sits above windows; with trackFullscreen off it stays
        // visible over a fullscreen app (browser, game) too. The area is
        // non-reactive, so it never swallows a click.
        Main.layoutManager.addTopChrome(this._area, {
            affectsStruts: false,
            trackFullscreen: false,
        });

        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbus.export(Gio.DBus.session,
            '/org/gnome/Shell/Extensions/MidscrollIndicator');
    }

    disable() {
        this._stop();
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
        if (this._area) {
            Main.layoutManager.removeChrome(this._area);
            this._area.destroy();
            this._area = null;
        }
    }

    // D-Bus: SetState(active, dx, dy)
    SetState(active, dx, dy) {
        if (!this._area)
            return;
        if (!active) {
            this._stop();
            return;
        }
        this._lastCall = GLib.get_monotonic_time();
        this._dx = dx;
        this._dy = dy;
        if (!this._active) {
            // The pointer is pinned for the whole drag, so wherever it is
            // right now is the anchor.
            const [x, y] = global.get_pointer();
            this._area.set_position(Math.round(x - SIZE / 2),
                Math.round(y - SIZE / 2));
            this._area.show();
            this._active = true;
            this._setUnredirect(false);
            this._setPointerHidden(true);
            this._watchdog = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1000,
                () => this._checkWatchdog());
        }
        this._area.queue_repaint();
    }

    _checkWatchdog() {
        const quiet = (GLib.get_monotonic_time() - this._lastCall) / 1000;
        if (quiet > WATCHDOG_MS) {
            this._watchdog = 0;
            this._stop();
            return GLib.SOURCE_REMOVE;
        }
        return GLib.SOURCE_CONTINUE;
    }

    _stop() {
        if (this._watchdog) {
            GLib.source_remove(this._watchdog);
            this._watchdog = 0;
        }
        this._active = false;
        this._dx = 0;
        this._dy = 0;
        if (this._area)
            this._area.hide();
        this._setPointerHidden(false);
        this._setUnredirect(true);
    }

    // A maximised frameless window (Discord and other Electron apps under
    // XWayland) covers the whole monitor, so mutter stops compositing and
    // shell overlays vanish. Keep compositing on while the marker is up.
    _setUnredirect(allowed) {
        if (allowed === !this._unredirectOff)
            return;
        try {
            if (allowed)
                global.compositor.enable_unredirect();
            else
                global.compositor.disable_unredirect();
            this._unredirectOff = !allowed;
        } catch (e) {
            console.warn(`midscroll-indicator: unredirect control: ${e}`);
        }
    }

    _setPointerHidden(hide) {
        if (!HIDE_POINTER || hide === this._pointerHidden)
            return;
        try {
            const backend = global.stage.get_context().get_backend();
            const tracker = backend.get_cursor_tracker?.() ??
                global.backend.get_cursor_tracker();
            const seat = backend.get_default_seat();
            if (hide) {
                // Hiding the sprite also drops the seat's unfocus inhibit
                // (mutter treats a hidden pointer as touch mode), so the
                // window under it would stop getting scroll. Take an
                // extra inhibit first, so the count never reaches zero.
                seat.inhibit_unfocus();
                tracker.inhibit_cursor_visibility();
            } else {
                tracker.uninhibit_cursor_visibility();
                seat.uninhibit_unfocus();
            }
            this._pointerHidden = hide;
        } catch (e) {
            // No such control in this shell version: just leave the real
            // pointer showing under the marker.
            console.warn(`midscroll-indicator: cannot hide pointer: ${e}`);
        }
    }

    _paint(area) {
        const cr = area.get_context();
        const c = SIZE / 2;

        // The disc.
        cr.arc(c, c, RADIUS, 0, 2 * Math.PI);
        cr.setSourceRGBA(0.12, 0.12, 0.13, 0.86);
        cr.fillPreserve();
        cr.setSourceRGBA(1, 1, 1, 0.38);
        cr.setLineWidth(1.5);
        cr.stroke();

        // One arrow per direction; the dragged way lights and grows.
        const dirs = [
            [0, -1, Math.max(0, -this._dy)],   // up
            [1, 0, Math.max(0, this._dx)],     // right
            [0, 1, Math.max(0, this._dy)],     // down
            [-1, 0, Math.max(0, -this._dx)],   // left
        ];
        for (const [ux, uy, mag] of dirs) {
            const lit = Math.min(1, Math.max(0, (mag - DEAD) / (FULL - DEAD)));
            const gap = 11 + 6 * lit;      // disc centre to arrow base
            const half = 6 + 3 * lit;      // half the base width
            const len = 8 + 4 * lit;       // base to tip
            const bx = c + ux * gap, by = c + uy * gap;
            const px = -uy, py = ux;       // perpendicular to the direction
            cr.moveTo(bx + ux * len, by + uy * len);
            cr.lineTo(bx + px * half, by + py * half);
            cr.lineTo(bx - px * half, by - py * half);
            cr.closePath();
            if (lit > 0)
                cr.setSourceRGBA(0.45, 0.82, 0.48, 0.6 + 0.4 * lit);
            else
                cr.setSourceRGBA(1, 1, 1, 0.5);
            cr.fill();
        }
        cr.$dispose();
    }
}
