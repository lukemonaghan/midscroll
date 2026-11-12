import Gio from 'gi://Gio';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE_XML = `
<node>
  <interface name="org.gnome.Shell.Extensions.MidscrollFocus">
    <method name="GetFocusedWindowClass">
      <arg type="s" direction="out" name="wmClass"/>
    </method>
  </interface>
</node>`;

export default class MidscrollFocusExtension extends Extension {
    enable() {
        this._dbusImpl = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbusImpl.export(Gio.DBus.session,
            '/org/gnome/Shell/Extensions/MidscrollFocus');
    }

    disable() {
        this._dbusImpl.unexport();
        this._dbusImpl = null;
    }

    GetFocusedWindowClass() {
        const win = global.display.focus_window;
        return win ? (win.get_wm_class() || '') : '';
    }
}
