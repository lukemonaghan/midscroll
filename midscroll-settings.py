#!/usr/bin/python3
"""midscroll-settings - a small GTK4 GUI for midscroll's tunables.

Reads the current /etc/midscroll.conf (world-readable), lets you change
every setting - including toggle mode and which devices count as a mouse -
with sliders, switches and a text box, and writes it back through pkexec
(midscroll-apply), which restarts the daemon so the change takes effect
immediately - or, when only the app blacklist changed, just tells it to
reload that list. No terminal or config editing required.

This process is unprivileged and stays that way: it cannot open
/dev/input (those nodes are root's), so the device list is built from
/proc/bus/input/devices, which is world-readable, plus the by-id and
by-path symlink names. Nothing here talks to the daemon; the only
privileged step is handing validated KEY=VALUE arguments to
midscroll-apply through polkit.
"""

import json
import os
import re
import struct
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

CONFIG_PATH = "/etc/midscroll.conf"
# Written by midscroll-overlay: every window class you have focused, with
# when. It is how the blacklist picker knows which apps you really use.
SEEN_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "midscroll", "seen-windows.json")
# Process names that say nothing about which application is running.
GENERIC_PROCS = {"env", "sh", "bash", "dash", "zsh", "fish", "python",
                 "python3", "flatpak", "gtk-launch", "xdg-open", "pkexec",
                 "sudo", "gjs", "java", "node", "electron", "systemd"}
# What midscroll-apply keeps of a blacklist entry (it drops the rest).
_CLASS_JUNK = re.compile(r"[^\w.\- ]", re.ASCII)
PROC_DEVICES = "/proc/bus/input/devices"
DEV_DIR = "/dev/input"
PHYS_MARKER = "midscroll"

# Kernel input codes (linux/input-event-codes.h), fixed ABI.
BTN_MIDDLE = 0x112
REL_X, REL_Y = 0x00, 0x01
ABS_X, ABS_MT_POSITION_X = 0x00, 0x35
KEY_A, KEY_Z, KEY_SPACE, KEY_ENTER = 30, 44, 57, 28
KEYBOARD_KEYS = (KEY_A, KEY_Z, KEY_SPACE, KEY_ENTER)
BITS_PER_WORD = struct.calcsize("l") * 8

# key, label, lower, upper, step, digits
FLOATS = [
    ("DEADZONE_PX", "Dead zone (px)", 0, 200, 1, 0),
    ("SPEED_MULT", "Speed multiplier", 0.001, 1.0, 0.001, 3),
    ("SPEED_EXP", "Speed curve exponent", 0.5, 4.0, 0.1, 1),
    ("MAX_PX_PER_SEC", "Max speed (px/s)", 1000, 200000, 500, 0),
    ("PX_PER_NOTCH", "Pixels per wheel notch", 1, 500, 1, 0),
    ("MAX_DRAG_PX", "Max drag distance (px)", 100, 10000, 10, 0),
    ("TICK_HZ", "Event rate (Hz)", 10, 360, 5, 0),
    ("GHOST_SCALE", "Ghost cursor travel", 0.1, 5.0, 0.1, 1),
]
# key, label, subtitle
BOOLS = [
    ("TOGGLE_MODE", "Toggle mode",
     "Click the middle button to start autoscroll and again to stop, "
     "instead of holding and dragging."),
    ("NATURAL", "Natural scrolling", "Invert the scroll direction."),
    ("GHOST_CURSOR", "Ghost cursor",
     "While you drag, draw a cursor that follows your hand from the "
     "anchor point. It only draws - the real pointer stays put so the "
     "scroll can't leak into another window."),
    ("DESKTOP_SCROLL", "Enable on desktop & panels",
     "Allow autoscroll while the desktop or a panel/taskbar is focused. "
     "Off by default so a middle-drag doesn't hijack the desktop."),
]
DEFAULTS = {
    "DEADZONE_PX": 15.0, "SPEED_MULT": 0.008, "SPEED_EXP": 2.2,
    "MAX_PX_PER_SEC": 30000.0, "PX_PER_NOTCH": 55.0, "MAX_DRAG_PX": 1200.0,
    "TICK_HZ": 90.0, "GHOST_SCALE": 1.0, "NATURAL": False,
    "TOGGLE_MODE": False, "DESKTOP_SCROLL": False, "GHOST_CURSOR": True,
    "BLACKLIST": "freecad, orcaslicer, minecraft",
    "EXTRA_DEVICES": "", "IGNORE_DEVICES": "",
    # Not settable here: only a root edit of the config turns it on.
    "ALLOW_KEYBOARDS": False,
}
FLOAT_KEYS = [f[0] for f in FLOATS]
BOOL_KEYS = [b[0] for b in BOOLS]
DEVICE_KEYS = ["EXTRA_DEVICES", "IGNORE_DEVICES"]


def read_config(path=CONFIG_PATH):
    """Current values from the config file, defaults filling any gaps."""
    values = dict(DEFAULTS)
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        k, v = (p.strip() for p in line.split("=", 1))
        if k in FLOAT_KEYS:
            try:
                values[k] = float(v)
            except ValueError:
                pass
        elif k in BOOL_KEYS or k == "ALLOW_KEYBOARDS":
            values[k] = v.lower() in ("1", "true", "yes", "on")
        elif k == "BLACKLIST" or k in DEVICE_KEYS:
            values[k] = v
    return values


def split_specs(text):
    """A config list value as a list of specs."""
    return [p.strip() for p in str(text).split(",") if p.strip()]


def _mask(value):
    """A /proc capability bitmap as one integer.

    The kernel prints these most-significant word first, one word per
    machine long, so pad each word out before joining them.
    """
    words = value.split()
    if not words:
        return 0
    return int("".join(w.zfill(BITS_PER_WORD // 4) for w in words), 16)


def _has(mask, bit):
    return bool(mask >> bit & 1)


def read_input_devices(path=PROC_DEVICES):
    """Every input device the kernel knows about, from /proc.

    One dict per device with the fields the daemon decides on: name,
    vendor/product, phys, its /dev/input/eventN node and the capability
    bits. midscroll's own uinput mirrors carry a known phys string and
    are left out - they aren't hardware and must never be selected.
    """
    try:
        with open(path) as f:
            blocks = f.read().split("\n\n")
    except OSError:
        return []
    devices = []
    for block in blocks:
        rec = {"name": "", "vendor": 0, "product": 0, "phys": "",
               "event": "", "keys": 0, "rels": 0, "abss": 0}
        for line in block.splitlines():
            if line.startswith("I: "):
                ids = dict(p.split("=", 1) for p in line[3:].split()
                           if "=" in p)
                try:
                    rec["vendor"] = int(ids.get("Vendor", "0"), 16)
                    rec["product"] = int(ids.get("Product", "0"), 16)
                except ValueError:
                    pass
            elif line.startswith("N: Name="):
                rec["name"] = line[len("N: Name="):].strip().strip('"')
            elif line.startswith("P: Phys="):
                rec["phys"] = line[len("P: Phys="):].strip()
            elif line.startswith("H: Handlers="):
                for handler in line[len("H: Handlers="):].split():
                    if handler.startswith("event"):
                        rec["event"] = f"{DEV_DIR}/{handler}"
            elif line.startswith("B: KEY="):
                rec["keys"] = _mask(line[len("B: KEY="):])
            elif line.startswith("B: REL="):
                rec["rels"] = _mask(line[len("B: REL="):])
            elif line.startswith("B: ABS="):
                rec["abss"] = _mask(line[len("B: ABS="):])
        if rec["event"] and PHYS_MARKER not in rec["phys"]:
            devices.append(rec)
    return devices


def is_mouse_record(rec):
    """The daemon's is_mouse() rule, applied to a /proc record."""
    return (_has(rec["keys"], BTN_MIDDLE)
            and _has(rec["rels"], REL_X)
            and _has(rec["rels"], REL_Y)
            and not _has(rec["abss"], ABS_X)
            and not _has(rec["abss"], ABS_MT_POSITION_X))


def is_keyboard_record(rec):
    """The daemon's is_keyboard() rule, applied to a /proc record."""
    return all(_has(rec["keys"], k) for k in KEYBOARD_KEYS)


def has_axes(rec):
    return bool(rec["rels"] or rec["abss"])


def stable_specs():
    """Map each /dev/input/eventN to its most stable identifier.

    Same preference as the daemon's --list-devices: the by-id name if
    there is one, else by-path, both of which survive reboots and
    renumbering where a bare eventN does not.
    """
    specs = {}
    for directory in (f"{DEV_DIR}/by-path", f"{DEV_DIR}/by-id"):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in sorted(names):
            link = os.path.join(directory, name)
            target = os.path.realpath(link)
            if target.startswith(f"{DEV_DIR}/event"):
                specs[target] = link  # by-id listed last, so it wins
    return specs


def spec_for(rec, specs, devices):
    """The identifier to write for one device: stable forms first.

    A by-id or by-path link if the device has one, else its name if that
    is unambiguous and survives the config's character set (no commas or
    other separators), else the bare event node - which works but moves
    around between reboots.
    """
    if rec["event"] in specs:
        return specs[rec["event"]]
    name = rec["name"].strip()
    same = [d for d in devices if d["name"].strip() == name]
    if len(name) >= 3 and len(same) == 1 and re.match(r"^[\w./:\- ]+$", name):
        return name
    return rec["event"]


def spec_matches(rec, spec):
    """The daemon's device_matches() rule, applied to a /proc record."""
    if spec.startswith("/"):
        return os.path.realpath(spec) == rec["event"]
    if re.match(r"^[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}$", spec):
        vendor, product = spec.split(":")
        return (rec["vendor"] == int(vendor, 16)
                and rec["product"] == int(product, 16))
    return spec.lower() in rec["name"].strip().lower()


def clean_class(text):
    """A window class reduced to what midscroll-apply will accept."""
    return _CLASS_JUNK.sub("", str(text).strip()).lower()[:64]


def _tokens(*names):
    """Lower-cased process-name guesses for an app, junk removed."""
    out = set()
    for name in names:
        n = (name or "").strip().lower()
        if not n:
            continue
        out.add(n)
        out.add(n.rsplit(".", 1)[-1])      # org.gnome.Ptyxis -> ptyxis
        if "." not in n:
            out.add(n.split("-", 1)[0])    # brave-browser -> brave
    return {t for t in out if len(t) >= 3} - GENERIC_PROCS


def read_seen():
    """{window class: last focused (epoch seconds)} from the overlay."""
    try:
        with open(SEEN_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    seen = {}
    if isinstance(data, dict):
        for cls, info in data.items():
            if isinstance(cls, str) and isinstance(info, dict):
                last = info.get("last", 0)
                seen[cls] = float(last) if isinstance(last, (int, float)) \
                    else 0.0
    return seen


def running_names():
    """Names of this user's running programs, lower-cased. Best effort.

    GNOME won't list windows to a normal client (Introspect.GetWindows is
    refused), so "is it open" is judged from /proc instead: the process
    name and the basename of its argv[0]. Good enough to float the apps
    you have running to the top; it can miss an oddly named launcher.
    """
    names = set()
    uid = os.getuid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return names
    for pid in pids:
        base = f"/proc/{pid}"
        try:
            if os.stat(base).st_uid != uid:
                continue
            with open(f"{base}/comm") as f:
                names.add(f.read().strip().lower())
            with open(f"{base}/cmdline", "rb") as f:
                argv0 = f.read(512).split(b"\0", 1)[0]
            if argv0:
                names.add(os.path.basename(
                    argv0.decode("utf-8", "replace")).lower())
        except OSError:
            continue
    names.discard("")
    return names - GENERIC_PROCS


def ago(ts):
    """'3 min ago' style age of an epoch timestamp."""
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


class AppEntry:
    """One app the picker can offer."""

    def __init__(self, wclass, name, icon=None, tokens=(), kind="installed"):
        self.shown = wclass            # the class as reported / guessed
        self.wclass = wclass.lower()   # what the daemon matches against
        self.name = name
        self.icon = icon
        self.tokens = set(tokens) | _tokens(wclass)
        self.kind = kind               # installed | seen | custom
        self.last = 0.0                # last focused, epoch seconds
        self.is_open = False


def installed_apps():
    """Every launchable desktop app, with its best guess at a window class.

    A StartupWMClass wins; otherwise the desktop-file id, which is what a
    Wayland app reports as its app_id. A class the user has actually
    focused later replaces the guess (see BlacklistPicker.refresh).
    """
    apps = []
    for info in Gio.AppInfo.get_all():
        try:
            if not info.should_show():
                continue
            did = info.get_id() or ""
            stem = did[:-8] if did.endswith(".desktop") else did
            wm = (info.get_startup_wm_class()
                  if isinstance(info, Gio.DesktopAppInfo) else None)
            wclass = (wm or stem).strip()
            if not wclass:
                continue
            exe = os.path.basename(info.get_executable() or "")
            apps.append(AppEntry(
                wclass, info.get_name() or wclass, info.get_icon(),
                _tokens(stem, wm, exe)))
        except (GLib.Error, TypeError):
            continue
    return apps


def find_apply():
    """Locate the privileged writer (installed, or alongside this file).

    The installed /usr/bin/midscroll-apply is checked first, deliberately: it
    is root-owned, so it is preferred over a sibling copy that might sit in a
    user-writable checkout. The checkout paths are only a dev fallback.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    for cand in ("/usr/bin/midscroll-apply",
                 os.path.join(here, "midscroll-apply"),
                 os.path.join(here, "midscroll-apply.py")):
        if os.path.exists(cand):
            return cand
    return "/usr/bin/midscroll-apply"


CSS = b"""
.nav-badge { border-radius: 12px; padding: 10px; color: white; }
.nav-badge.blue   { background: #3b82f6; }
.nav-badge.orange { background: #e8890c; }
.nav-badge.green  { background: #22a06b; }
.nav-badge.purple { background: #8b5cf6; }
.nav-title { font-size: 1.1em; }
.navigation-sidebar row { padding: 10px 12px; margin: 3px 8px;
                          border-radius: 14px; }
/* Some GTK themes (a dark "Green-Dark", for one) paint a switched-on switch
   nearly the same as its track, so on and off look alike. (Such themes
   build their accent from var(), which GTK can't resolve into the usual
   @accent_bg_color; plain @accent_color survives.) The `window` prefix
   and state variants outrank the theme's own, more specific, rules. */
window switch:checked, window switch:checked:hover,
window switch:checked:backdrop {
  background-color: @accent_color; background-image: none; }
window switch:checked slider { background: white; }
"""


def install_css():
    """Style the sidebar badges once per process."""
    if getattr(install_css, "done", False):
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    install_css.done = True


class AppRow(Gtk.ListBoxRow):
    """A picker row: .entry is its AppEntry, .switch and .sub its widgets."""


class BlacklistPicker(Gtk.Box):
    """Searchable app list: switch an app on to pause midscroll over it.

    Rows are grouped so the useful ones come first: apps already paused,
    then apps open right now, then ones you've used lately (most recent
    first), then every other installed app. The search box filters by app
    name or window class, and offers to add a class that isn't listed.

    The value is still the config's comma-separated list of lower-cased
    window-class substrings (get_text / set_text), so nothing downstream
    changes. A row is "on" when any entry in that list is a substring of
    its class - exactly the daemon's rule - so switching a row off removes
    whatever entry was matching it.
    """

    GROUPS = ("Paused over these apps", "Open now", "Recently used",
              "Installed apps")

    def __init__(self, on_change):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.on_change = on_change
        self.blocked = []
        self.rows = []
        self._syncing = False
        self._query = ""

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search = Gtk.SearchEntry(hexpand=True)
        self.search.set_placeholder_text("Search apps, or type a window class")
        self.search.connect("search-changed", self._on_search)
        bar.append(self.search)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Re-check which apps are open")
        refresh.connect("clicked", lambda _b: self.refresh())
        bar.append(refresh)
        self.append(bar)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.set_sort_func(self._sort)
        self.listbox.set_filter_func(self._filter)
        self.listbox.set_header_func(self._header)
        self.listbox.connect(
            "row-activated",
            lambda _l, row: row.switch.set_active(not row.switch.get_active()))
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.listbox)
        frame = Gtk.Frame(vexpand=True)
        frame.set_child(scroller)
        self.append(frame)

        self.add_btn = Gtk.Button(halign=Gtk.Align.START)
        self.add_btn.set_visible(False)
        self.add_btn.connect("clicked", self._on_add_custom)
        self.append(self.add_btn)

        for entry in installed_apps():
            self._add(entry)

    # ---- value, as the config stores it ----
    def get_text(self):
        return ", ".join(self.blocked)

    def set_text(self, text):
        self.blocked = []
        for part in str(text).split(","):
            c = clean_class(part)
            if c and c not in self.blocked:
                self.blocked.append(c)
        for row in list(self.rows):
            if row.entry.kind == "custom":
                self._remove(row)
        self._ensure_customs()
        self.refresh()

    # ---- rows ----
    def _add(self, entry):
        row = AppRow()
        row.entry = entry
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(10)
        box.set_margin_end(10)
        icon = Gtk.Image(pixel_size=32)
        if entry.icon is not None:
            icon.set_from_gicon(entry.icon)
        else:
            icon.set_from_icon_name("application-x-executable")
        box.append(icon)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                      valign=Gtk.Align.CENTER)
        col.append(Gtk.Label(label=entry.name, xalign=0,
                             ellipsize=Pango.EllipsizeMode.END))
        row.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        row.sub.add_css_class("dim-label")
        row.sub.add_css_class("caption")
        col.append(row.sub)
        box.append(col)
        row.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        row.switch.connect("notify::active", self._on_switch, row)
        box.append(row.switch)
        row.set_child(box)
        self.listbox.append(row)
        self.rows.append(row)
        self._describe(row)
        self._syncing = True
        row.switch.set_active(self._is_blocked(entry.wclass))
        self._syncing = False
        return row

    def _remove(self, row):
        self.listbox.remove(row)
        if row in self.rows:
            self.rows.remove(row)

    def _describe(self, row):
        e = row.entry
        bits = [e.shown]
        if e.is_open:
            bits.append("open now")
        elif e.last:
            bits.append("used " + ago(e.last))
        elif e.kind == "custom":
            bits.append("custom match")
        row.sub.set_text(" · ".join(bits))

    def _ensure_customs(self):
        """A row for each blacklist entry that no known app matches."""
        for b in self.blocked:
            if not any(b in r.entry.wclass for r in self.rows):
                self._add(AppEntry(b, b, None, (), kind="custom"))

    def _row_for(self, key):
        """The row for a window class: exact match first, then by token."""
        for row in self.rows:
            if row.entry.wclass == key:
                return row
        for row in self.rows:
            if key in row.entry.tokens:
                return row
        return None

    # ---- state ----
    def _is_blocked(self, wclass):
        return any(b in wclass for b in self.blocked)

    def _group(self, entry):
        if self._is_blocked(entry.wclass):
            return 0
        if entry.is_open:
            return 1
        return 2 if entry.last else 3

    def _resync(self):
        """Bring every switch, and the ordering, in line with the list."""
        self._syncing = True
        for row in self.rows:
            row.switch.set_active(self._is_blocked(row.entry.wclass))
        self._syncing = False
        self.listbox.invalidate_sort()
        self.listbox.invalidate_headers()

    def refresh(self):
        """Re-read which apps are open and which you've used."""
        seen = read_seen()
        procs = running_names()
        for cls, last in seen.items():
            key = cls.lower()
            row = self._row_for(key)
            if row is None:
                row = self._add(AppEntry(cls, cls, None, (), kind="seen"))
            e = row.entry
            if e.kind == "custom":
                e.kind, e.name = "seen", cls     # now a real, known app
            if e.kind == "installed" and e.wclass != key:
                # What the shell reports beats our guess from the .desktop.
                e.shown, e.wclass = cls, key
                e.tokens |= _tokens(cls)
            e.last = max(e.last, last)
        for row in self.rows:
            e = row.entry
            e.is_open = bool(e.tokens & procs) or e.wclass in procs
            self._describe(row)
        self._resync()

    def _toggle(self, entry, on):
        if on:
            c = clean_class(entry.shown)
            if c and not self._is_blocked(entry.wclass):
                self.blocked.append(c)
        else:
            self.blocked = [b for b in self.blocked if b not in entry.wclass]
        GLib.idle_add(self._drop_stale_custom)
        self._resync()
        self.on_change()

    def _drop_stale_custom(self):
        for row in list(self.rows):
            if row.entry.kind == "custom" \
                    and not self._is_blocked(row.entry.wclass):
                self._remove(row)
        return False

    # ---- signals ----
    def _on_switch(self, switch, _pspec, row):
        if not self._syncing:
            self._toggle(row.entry, switch.get_active())

    def _on_search(self, entry):
        self._query = entry.get_text().strip().lower()
        self.listbox.invalidate_filter()
        q = clean_class(self._query)
        if q and not any(r.entry.wclass == q for r in self.rows):
            self.add_btn.set_label(
                f"Pause over any window whose class contains “{q}”")
            self.add_btn.set_visible(True)
        else:
            self.add_btn.set_visible(False)

    def _on_add_custom(self, _btn):
        q = clean_class(self._query)
        if q and q not in self.blocked:
            self.blocked.append(q)
            self._ensure_customs()
        self.search.set_text("")
        self._resync()
        self.on_change()

    # ---- list plumbing ----
    def _key(self, row):
        e = row.entry
        g = self._group(e)
        return (g, -e.last if g < 3 else 0.0, e.name.lower())

    def _sort(self, a, b):
        ka, kb = self._key(a), self._key(b)
        return (ka > kb) - (ka < kb)

    def _filter(self, row):
        q = self._query
        return not q or q in row.entry.name.lower() or q in row.entry.wclass

    def _header(self, row, before):
        group = self._group(row.entry)
        if before is None or self._group(before.entry) != group:
            label = Gtk.Label(label=self.GROUPS[group], xalign=0)
            label.add_css_class("heading")
            label.set_margin_top(8)
            label.set_margin_bottom(4)
            label.set_margin_start(10)
            row.set_header(label)
        else:
            row.set_header(None)


class Window(Gtk.ApplicationWindow):
    # key, title, subtitle, icon, badge colour
    PAGES = (
        ("behavior", "Behavior", "Toggle mode \u2022 Direction",
         "preferences-system-symbolic", "blue"),
        ("speed", "Speed and feel", "Dead zone \u2022 Speed curve",
         "utilities-system-monitor-symbolic", "orange"),
        ("devices", "Mice and devices", "Which mice midscroll uses",
         "input-mouse-symbolic", "green"),
        ("apps", "Apps to pause over", "", "view-app-grid-symbolic", "purple"),
    )

    def __init__(self, app):
        super().__init__(application=app, title="midscroll")
        self.set_default_size(940, 680)
        self.floats = {}
        self.bools = {}
        self.device_rows = []   # (record, spec, auto-detected, switch)
        self.extra = []         # EXTRA_DEVICES as loaded
        self.ignore = []        # IGNORE_DEVICES as loaded
        self.allow_keyboards = False
        self.nav_subs = {}      # page key -> its subtitle label
        install_css()

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        reset = Gtk.Button(label="Reset to defaults")
        reset.connect("clicked", self.on_reset)
        header.pack_start(reset)
        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", self.on_apply)
        header.pack_end(self.apply_btn)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)
        panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        outer.append(panes)

        # Left: the groups. Right: one group's page at a time, so each page
        # has exactly one scroller and nothing scrolls inside anything.
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.nav.add_css_class("navigation-sidebar")
        side = Gtk.ScrolledWindow()
        side.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side.set_size_request(300, -1)
        side.set_child(self.nav)
        panes.append(side)
        panes.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self.stack = Gtk.Stack(hexpand=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        panes.append(self.stack)

        self.blacklist = BlacklistPicker(self._on_blacklist_change)
        pages = {
            "behavior": (self._page_behavior, True),
            "speed": (self._page_speed, True),
            "devices": (self._page_devices, True),
            "apps": (self._page_apps, False),   # the list is its own scroller
        }
        for key, title, sub, icon, colour in self.PAGES:
            self.nav.append(self._nav_row(key, title, sub, icon, colour))
            build, scrolls = pages[key]
            self.stack.add_named(self._page(title, build(), scrolls), key)
        self.nav.connect("row-selected", self._on_nav)
        self.nav.select_row(self.nav.get_row_at_index(0))

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim-label")
        self.status.set_margin_start(16)
        self.status.set_margin_end(16)
        self.status.set_margin_top(6)
        self.status.set_margin_bottom(10)
        self.status.set_wrap(True)
        outer.append(self.status)

        # Coming back to this window means you've probably just used other
        # apps; re-check what's open and recent so they're at the top.
        self.connect("notify::is-active",
                     lambda w, _p: w.is_active() and w.blacklist.refresh())
        self.load()

    # ---- layout: sidebar and pages ----
    def _nav_row(self, key, title, sub, icon, colour):
        row = Gtk.ListBoxRow()
        row.key = key
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        badge = Gtk.Image(icon_name=icon, pixel_size=20,
                          halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        badge.add_css_class("nav-badge")
        badge.add_css_class(colour)
        box.append(badge)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                      valign=Gtk.Align.CENTER)
        name = Gtk.Label(label=title, xalign=0, wrap=True)
        name.add_css_class("nav-title")
        col.append(name)
        subtitle = Gtk.Label(label=sub, xalign=0, wrap=True)
        subtitle.add_css_class("dim-label")
        subtitle.add_css_class("caption")
        col.append(subtitle)
        self.nav_subs[key] = subtitle
        box.append(col)
        row.set_child(box)
        return row

    def _page(self, title, body, scrolls):
        """A right-pane page: big title, then the body (one scroller)."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(18)
        page.set_margin_bottom(8)
        page.set_margin_start(24)
        page.set_margin_end(24)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("title-1")
        page.append(heading)
        if scrolls:
            scroller = Gtk.ScrolledWindow(vexpand=True)
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_child(body)
            page.append(scroller)
        else:
            body.set_vexpand(True)
            page.append(body)
        return page

    def _on_nav(self, _listbox, row):
        if row is not None:
            self.stack.set_visible_child_name(row.key)

    def _page_body(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(4)
        box.set_margin_bottom(12)
        box.set_margin_end(12)
        return box

    def _page_behavior(self):
        box = self._page_body()
        for key, label, subtitle in BOOLS:
            box.append(self._bool_row(key, label, subtitle))
        return box

    def _page_speed(self):
        box = self._page_body()
        for key, label, lo, hi, step, digits in FLOATS:
            box.append(self._float_row(key, label, lo, hi, step, digits))
        return box

    def _page_devices(self):
        box = self._page_body()
        box.append(self._devices_section())
        return box

    def _page_apps(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hint = Gtk.Label(
            label="Switch an app on and midscroll stays out of its way "
                  "while it's focused - for apps with their own "
                  "middle-drag (CAD, design tools, games). Apps you have "
                  "open or used recently are listed first. Applying a "
                  "change here takes effect at once, with no restart.",
            wrap=True, xalign=0)
        hint.add_css_class("dim-label")
        box.append(hint)
        self.blacklist.set_vexpand(True)
        box.append(self.blacklist)
        return box

    def _on_blacklist_change(self):
        self.set_status("Blacklist changed - press Apply.")
        self._update_counts()

    def _update_counts(self):
        n = len(self.blacklist.blocked)
        self.nav_subs["apps"].set_text(
            (f"{n} paused" if n else "None paused")
            + " \u2022 Open apps \u2022 Search")

    # ---- widget builders ----
    def _heading(self, text):
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.add_css_class("heading")
        lbl.set_margin_top(8)
        return lbl

    def _row(self, label):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        text = Gtk.Label(label=label, xalign=0, hexpand=True)
        row.append(text)
        return row

    def _float_row(self, key, label, lo, hi, step, digits):
        row = self._row(label)
        adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step,
                             page_increment=step * 10)
        spin = Gtk.SpinButton(adjustment=adj, digits=digits)
        spin.set_valign(Gtk.Align.CENTER)
        self.floats[key] = spin
        row.append(spin)
        return row

    def _devices_section(self):
        """The list of input devices, with a switch each."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hint = Gtk.Label(
            label="midscroll picks up ordinary mice by itself. Turn a "
                  "device on to use it anyway, or off to leave it alone. "
                  "Only a device with a middle button and relative axes "
                  "can actually autoscroll; anything you turn on here is "
                  "grabbed exclusively and re-emitted through a virtual "
                  "mirror.",
            wrap=True, xalign=0, hexpand=True)
        hint.add_css_class("dim-label")
        head.append(hint)
        rescan = Gtk.Button(label="Rescan")
        rescan.set_valign(Gtk.Align.START)
        rescan.connect("clicked", lambda _b: self.rebuild_devices())
        head.append(rescan)
        box.append(head)

        # The page itself scrolls; the list just grows with the devices.
        self.devices_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                   spacing=10)
        self.devices_box.set_margin_top(6)
        box.append(self.devices_box)

        self.show_all_devices = Gtk.CheckButton(
            label="Show all input devices")
        self.show_all_devices.connect("toggled",
                                      lambda _c: self.rebuild_devices())
        box.append(self.show_all_devices)
        return box

    def _device_row(self, rec, spec, auto, on, blocked):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        col.append(Gtk.Label(label=rec["name"].strip() or "(unnamed)",
                             xalign=0, wrap=True))
        if blocked:
            state = "keyboard - only a root edit of the config can allow it"
        elif auto:
            state = "detected as a mouse"
        else:
            state = "not detected as a mouse"
        sub = Gtk.Label(label=f"{rec['event']} - {state}\n{spec}",
                        xalign=0, wrap=True, selectable=True)
        sub.add_css_class("dim-label")
        col.append(sub)
        row.append(col)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        switch.set_active(on)
        if blocked:
            switch.set_sensitive(False)
            switch.set_tooltip_text(
                "midscroll never grabs a keyboard unless ALLOW_KEYBOARDS "
                f"is set in {CONFIG_PATH}, which only root can do.")
        row.append(switch)
        self.device_rows.append((rec, spec, auto, switch))
        return row

    def _orphan_row(self, spec, in_extra):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = Gtk.Label(
            label=f"{spec}\n{'use as a mouse' if in_extra else 'never use'} "
                  "- not connected",
            xalign=0, hexpand=True, wrap=True)
        label.add_css_class("dim-label")
        row.append(label)
        drop = Gtk.Button(icon_name="list-remove-symbolic")
        drop.set_valign(Gtk.Align.CENTER)
        drop.set_tooltip_text("Forget this device")
        drop.connect("clicked", self.on_forget, spec, in_extra)
        row.append(drop)
        return row

    def on_forget(self, _btn, spec, in_extra):
        specs = self.extra if in_extra else self.ignore
        if spec in specs:
            specs.remove(spec)
        self.rebuild_devices()
        self.set_status(f"Removed {spec} - press Apply to save.")

    def rebuild_devices(self):
        """Redraw the device list from /proc and the current lists."""
        child = self.devices_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.devices_box.remove(child)
            child = nxt
        self.device_rows = []
        devices = read_input_devices()
        specs = stable_specs()
        show_all = self.show_all_devices.get_active()
        listed = set()
        for rec in devices:
            spec = spec_for(rec, specs, devices)
            auto = is_mouse_record(rec)
            named = [s for s in self.extra + self.ignore
                     if spec_matches(rec, s)]
            listed.update(named)
            if not (show_all or auto or has_axes(rec) or named):
                continue
            blocked = is_keyboard_record(rec) and not self.allow_keyboards
            on = (auto or any(spec_matches(rec, s) for s in self.extra))
            if any(spec_matches(rec, s) for s in self.ignore):
                on = False
            self.devices_box.append(
                self._device_row(rec, spec, auto, on and not blocked,
                                 blocked))
        # Specs for hardware that isn't plugged in stay in the config
        # until they are removed here, rather than vanishing silently.
        orphans = [(s, True) for s in self.extra if s not in listed]
        orphans += [(s, False) for s in self.ignore if s not in listed]
        if orphans:
            note = Gtk.Label(label="Set by hand, not connected right now:",
                             xalign=0)
            note.add_css_class("dim-label")
            self.devices_box.append(note)
            for spec, in_extra in orphans:
                self.devices_box.append(self._orphan_row(spec, in_extra))

    def _bool_row(self, key, label, subtitle):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        col.append(Gtk.Label(label=label, xalign=0))
        sub = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        col.append(sub)
        row.append(col)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.bools[key] = switch
        row.append(switch)
        return row

    # ---- data ----
    def load(self, values=None):
        vals = values if values is not None else read_config()
        for key, spin in self.floats.items():
            spin.set_value(float(vals[key]))
        for key, switch in self.bools.items():
            switch.set_active(bool(vals[key]))
        self.blacklist.set_text(str(vals["BLACKLIST"]))  # also refreshes
        self._update_counts()
        self.extra = split_specs(vals["EXTRA_DEVICES"])
        self.ignore = split_specs(vals["IGNORE_DEVICES"])
        # Read from the config, never written back from here.
        self.allow_keyboards = bool(vals["ALLOW_KEYBOARDS"])
        self.rebuild_devices()

    def on_reset(self, _btn):
        # Keep ALLOW_KEYBOARDS as it is: this window doesn't own it.
        values = dict(DEFAULTS)
        values["ALLOW_KEYBOARDS"] = self.allow_keyboards
        self.load(values)
        self.set_status("Defaults loaded - press Apply to save.")

    def device_lists(self):
        """(EXTRA_DEVICES, IGNORE_DEVICES) from the switches on screen.

        A switch only produces an entry when it disagrees with what
        midscroll would do on its own, so the lists stay as short as the
        change actually is. Specs whose device isn't connected are kept.
        """
        extra, ignore = [], []
        seen = set()
        for rec, spec, auto, switch in self.device_rows:
            on = switch.get_active()
            if on and not auto:
                extra.append(spec)
            elif auto and not on:
                ignore.append(spec)
            for s in self.extra + self.ignore:
                if spec_matches(rec, s):
                    seen.add(s)
        extra += [s for s in self.extra if s not in seen]
        ignore += [s for s in self.ignore if s not in seen]
        return extra, ignore

    def collect(self):
        args = []
        for key, spin in self.floats.items():
            args.append(f"{key}={spin.get_value():g}")
        for key, switch in self.bools.items():
            args.append(f"{key}={'true' if switch.get_active() else 'false'}")
        args.append(f"BLACKLIST={self.blacklist.get_text()}")
        extra, ignore = self.device_lists()
        args.append("EXTRA_DEVICES=" + ", ".join(extra))
        args.append("IGNORE_DEVICES=" + ", ".join(ignore))
        return args

    def set_status(self, text):
        self.status.set_text(text)

    # ---- apply ----
    def on_apply(self, _btn):
        argv = ["pkexec"]
        apply = find_apply()
        if apply.endswith(".py"):
            argv += [sys.executable, apply]
        else:
            argv.append(apply)
        argv += self.collect()
        self.apply_btn.set_sensitive(False)
        self.set_status("Applying...")
        try:
            proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as err:
            self.apply_btn.set_sensitive(True)
            self.set_status(f"Could not run pkexec: {err.message}")
            return
        proc.communicate_utf8_async(None, None, self._applied, None)

    def _applied(self, proc, result, _data):
        self.apply_btn.set_sensitive(True)
        try:
            _ok, out, err = proc.communicate_utf8_finish(result)
        except GLib.Error as exc:
            self.set_status(f"Apply failed: {exc.message}")
            return
        if proc.get_exit_status() == 0:
            if "reloaded blacklist" in (out or ""):
                self.set_status("Applied - blacklist updated, no restart.")
            else:
                self.set_status("Applied and restarted midscroll.")
        else:
            detail = (err or "").strip().splitlines()
            msg = detail[-1] if detail else "authorization dismissed"
            self.set_status(f"Not applied: {msg}")


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.gnhen.midscroll.Settings")

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


def main():
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
