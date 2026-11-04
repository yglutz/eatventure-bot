"""Conservative X11 desktop backend. No desktop connections at import time."""
import os


class X11Backend:
    def __init__(self, window_title, target_width=800, target_height=600):
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            raise RuntimeError("Wayland/XWayland sessions are unsupported")
        if not os.environ.get("DISPLAY"):
            raise RuntimeError("An authenticated X11 DISPLAY is required")
        # python-xlib's supported initialization installs real request locks;
        # it must precede importing display/opening the shared connection.
        import Xlib.threaded  # noqa: F401
        from Xlib import X, display
        from Xlib.ext import xtest
        import mss
        self.X = X
        self.xtest = xtest
        self.display = display.Display()
        self.grabber = None
        self.last_capture_rect = None
        self.window_title = window_title
        self.target_width = target_width
        self.target_height = target_height
        try:
            if not self.display.has_extension("XTEST"):
                raise RuntimeError("XTEST is required")
            self.root = self.display.screen().root
            clients = self._property(self.root, "_NET_CLIENT_LIST", X.AnyPropertyType)
            matches = []
            for xid in clients:
                window = self.display.create_resource_object("window", int(xid))
                title = self._property(window, "_NET_WM_NAME", self._atom("UTF8_STRING"))
                if isinstance(title, bytes):
                    title = title.decode("utf-8", errors="strict")
                if title == window_title:
                    matches.append(window)
            if len(matches) != 1:
                raise RuntimeError("Expected exactly one exact-title target window")
            self.window = matches[0]
            self.hwnd = self.window.id  # Compatibility only: an XID, not a Win32 handle.
            self.validate()
            self.grabber = mss.mss()
        except Exception:
            self.close()
            raise

    def _atom(self, name):
        atom = self.display.intern_atom(name, only_if_exists=True)
        if not atom:
            raise RuntimeError("Required X11 property unavailable: " + name)
        return atom

    def _property(self, window, name, kind):
        prop = window.get_full_property(self._atom(name), kind)
        if prop is None:
            raise RuntimeError("Missing X11 property: " + name)
        return prop.value

    def _rect(self, window):
        geometry = window.get_geometry()
        translated = self.root.translate_coords(window, 0, 0)
        return translated.x, translated.y, geometry.width, geometry.height

    def get_window_rect(self):
        return self._rect(self.window)

    def find_window(self):
        # Never silently rebind a destroyed/replaced target.
        self.validate()

    def resize_window(self):
        self.validate()  # X11 deliberately never requests a resize.

    def validate(self, expected_rect=None):
        try:
            X = self.X
            if self.window.get_attributes().map_state != X.IsViewable:
                raise RuntimeError("Target is not viewable")
            active = self._property(self.root, "_NET_ACTIVE_WINDOW", X.AnyPropertyType)
            focus = self.display.get_input_focus().focus
            if len(active) != 1 or int(active[0]) != self.hwnd or getattr(focus, "id", None) != self.hwnd:
                raise RuntimeError("Target does not have verified focus")
            rect = self.get_window_rect()
            x, y, w, h = rect
            root = self.root.get_geometry()
            if w != self.target_width or h != self.target_height:
                raise RuntimeError("X11 client size must match calibrated requested size; resize manually")
            if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > root.width or y + h > root.height:
                raise RuntimeError("Target geometry is outside desktop bounds")
            if expected_rect is not None and rect != expected_rect:
                raise RuntimeError("Target geometry changed")
            # Root children include WM frames and override-redirect popups, unlike
            # _NET_CLIENT_LIST_STACKING alone. Require an identifiable ancestor.
            ancestor = self.window
            for _ in range(64):
                parent = ancestor.query_tree().parent
                if parent.id == self.root.id:
                    break
                ancestor = parent
            else:
                raise RuntimeError("Cannot resolve target window ancestry")
            children = self.root.query_tree().children  # bottom to top, XQueryTree
            ids = [child.id for child in children]
            if ancestor.id not in ids:
                raise RuntimeError("Cannot establish target stacking")
            for child in children[ids.index(ancestor.id) + 1:]:
                attrs = child.get_attributes()
                if attrs.map_state != X.IsViewable or attrs.win_class == X.InputOnly:
                    continue
                cx, cy, cw, ch = self._rect(child)
                if cx < x + w and cx + cw > x and cy < y + h and cy + ch > y:
                    raise RuntimeError("Target is obscured by another window")
            return rect
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Unable to validate X11 target") from exc

    def capture(self, max_y=None):
        import numpy as np
        self.last_capture_rect = None
        rect = self.validate()
        x, y, w, h = rect
        if max_y is not None:
            if not isinstance(max_y, int) or max_y <= 0:
                raise ValueError("max_y must be a positive integer")
            h = min(h, max_y)
        frame = np.asarray(self.grabber.grab(dict(left=x, top=y, width=w, height=h)))
        self.validate(expected_rect=rect)
        if frame.shape != (h, w, 4):
            raise RuntimeError("Unexpected capture dimensions")
        result = np.ascontiguousarray(frame[:, :, :3])
        self.last_capture_rect = rect  # Full client geometry, even for cropped frames.
        return result

    def is_window_active(self):
        try:
            self.validate()
            return True
        except Exception:
            return False

    def get_cursor_pos(self):
        pointer = self.root.query_pointer()
        return pointer.root_x, pointer.root_y

    def move_pointer(self, screen_x, screen_y):
        self.validate(self.last_capture_rect)
        self.xtest.fake_input(self.display, self.X.MotionNotify, x=int(screen_x), y=int(screen_y))
        self.display.sync()

    def button_down(self):
        self.validate(self.last_capture_rect)
        self.xtest.fake_input(self.display, self.X.ButtonPress, 1)
        self.display.sync()

    def button_up(self):
        # Release must remain possible after focus/geometry loss.
        self.xtest.fake_input(self.display, self.X.ButtonRelease, 1)
        self.display.sync()

    def close(self):
        try:
            if self.grabber is not None:
                self.grabber.close()
                self.grabber = None
        finally:
            if self.display is not None:
                self.display.close()
                self.display = None
