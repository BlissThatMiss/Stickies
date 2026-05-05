#!/usr/bin/env python3
import gi
import os
import json
import datetime
import socket
import threading
import traceback
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GdkPixbuf, Gdk, Pango, GLib
import cairo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
SOCKET_PATH = os.path.join(BASE_DIR, "stickies.sock")


# --- Settings persistence ---
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


def pixbuf_to_cairo_surface(pixbuf):
    """Convert a GdkPixbuf to a cairo ImageSurface."""
    has_alpha = pixbuf.get_has_alpha()
    fmt = cairo.FORMAT_ARGB32 if has_alpha else cairo.FORMAT_RGB24
    w, h = pixbuf.get_width(), pixbuf.get_height()
    surf = cairo.ImageSurface(fmt, w, h)
    cr = cairo.Context(surf)
    Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
    cr.paint()
    return surf


class NineSliceDrawingArea(Gtk.DrawingArea):
    """
    A single DrawingArea that renders the full 9-slice texture.
    Natural size is always 0x0 — it NEVER imposes a size floor,
    which is the key property that allows the window to shrink freely.
    """
    def __init__(self):
        super().__init__()
        self.pixbufs = None
        self._surfaces = {}
        self.connect("draw", self._on_draw)
        # Zero natural size — this is the whole point of using DrawingArea
        self.set_size_request(1, 1)

    def set_pixbufs(self, pixbufs):
        self.pixbufs = pixbufs
        self._surfaces = {}
        for k, pb in pixbufs.items():
            self._surfaces[k] = pixbuf_to_cairo_surface(pb)
        self.queue_draw()

    def _on_draw(self, widget, cr):
        if not self.pixbufs:
            return
        alloc = self.get_allocation()
        W, H = alloc.width, alloc.height

        pb = self.pixbufs
        # All layout values are kept as plain ints so every rectangle is
        # pixel-perfect with no sub-pixel gaps or overlaps at the seams.
        lw = int(pb["l"].get_width())
        rw = int(pb["r"].get_width())
        th = int(pb["t"].get_height())
        bh = int(pb["b"].get_height())
        mid_w = max(1, W - lw - rw)
        mid_h = max(1, H - th - bh)

        def draw_slice(key, dst_x, dst_y, dst_w, dst_h):
            """
            Draw a single slice scaled to (dst_w x dst_h) at (dst_x, dst_y).
            Uses a SurfacePattern with an explicit scaling matrix instead of
            cr.scale(), so the transform never leaks outside the clip rect and
            bilinear filtering cannot bleed edge pixels across seam boundaries.
            """
            src = self._surfaces[key]
            src_w = src.get_width()
            src_h = src.get_height()
            if dst_w <= 0 or dst_h <= 0:
                return
            pat = cairo.SurfacePattern(src)
            # Map source pixels → destination pixels via an inverse matrix
            # (cairo patterns are specified in source space)
            sx = src_w / dst_w
            sy = src_h / dst_h
            mat = cairo.Matrix()
            mat.scale(sx, sy)
            mat.translate(-dst_x, -dst_y)
            pat.set_matrix(mat)
            # FILTER_NEAREST is the key fix: bilinear interpolation samples pixels
            # outside the source boundary and bleeds them across seams, producing
            # the gray shadow lines. NEAREST has no such kernel — hard pixel edges.
            pat.set_filter(cairo.FILTER_NEAREST)
            pat.set_extend(cairo.EXTEND_PAD)
            cr.save()
            cr.rectangle(dst_x, dst_y, dst_w, dst_h)
            cr.clip()
            cr.set_source(pat)
            cr.paint()
            cr.restore()

        def draw_tiled_h(key, dst_x, dst_y, dst_w, dst_h):
            """Tile a slice horizontally (used for the top edge)."""
            src = self._surfaces[key]
            src_w = src.get_width()
            if dst_w <= 0 or dst_h <= 0:
                return
            cr.save()
            cr.rectangle(dst_x, dst_y, dst_w, dst_h)
            cr.clip()
            x = dst_x
            while x < dst_x + dst_w:
                cr.set_source_surface(src, x, dst_y)
                cr.get_source().set_filter(cairo.FILTER_NEAREST)
                cr.paint()
                x += src_w
            cr.restore()

        # Corners — drawn at their native pixel size, no scaling needed.
        draw_slice("tl", 0,      0,      lw,    th)
        draw_slice("tr", W - rw, 0,      rw,    th)
        draw_slice("bl", 0,      H - bh, lw,    bh)
        draw_slice("br", W - rw, H - bh, rw,    bh)

        # Edges — mid_w / mid_h are exact ints, so no fractional seams.
        draw_tiled_h("t", lw,     0,      mid_w, th)
        draw_slice("b",   lw,     H - bh, mid_w, bh)
        draw_slice("l",   0,      th,     lw,    mid_h)
        draw_slice("r",   W - rw, th,     rw,    mid_h)

        # Center
        draw_slice("center", lw, th, mid_w, mid_h)


class StickyNote(Gtk.Window):
    def __init__(self, color=None, font=None, start_active=True):
        super().__init__()

        self.set_title("Stickies")
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_icon_name("sticky")
        self.set_role("stickies-note")
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)

        settings = load_settings()
        self.color = color or settings.get("last_color", "yellow")
        self.font_desc = Pango.FontDescription(font or settings.get("last_font", "Geneva-Ori 6"))

        self.creation_date = datetime.datetime.now()
        self.modified_date = self.creation_date

        self.connect("button-press-event", self.on_button_press)
        self.connect("focus-in-event", self.on_focus_in)
        self.connect("focus-out-event", self.on_focus_out)

        css = b"""
        textview {
            background-color: transparent;
        }
        textview text {
            color: black;
            background-color: transparent;
        }
        #close-button {
            border: none;
            background: transparent;
            box-shadow: none;
            padding: 0;
        }
        """
        style_provider = Gtk.CssProvider()
        style_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            style_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.set_keep_above(False)

        self.load_textures()

        # Minimum window size derived from corner/edge pixel dims
        pb = self.pixbufs_active
        min_w = pb["l"].get_width() + pb["r"].get_width() + 1
        min_h = pb["t"].get_height() + pb["b"].get_height() + 1
        geometry = Gdk.Geometry()
        geometry.min_width = min_w
        geometry.min_height = min_h
        self.set_geometry_hints(self, geometry, Gdk.WindowHints.MIN_SIZE)
        self.set_size_request(min_w, min_h)

        # --- Layout ---
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)

        menubar = self.create_menubar()
        vbox.pack_start(menubar, False, False, 0)

        # Single DrawingArea for entire 9-slice background — zero natural size,
        # so it never prevents the window from shrinking.
        self.nine_slice = NineSliceDrawingArea()
        self.nine_slice.set_pixbufs(self.pixbufs)

        overlay = Gtk.Overlay()
        overlay.add(self.nine_slice)

        self.textview = Gtk.TextView()
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_margin_top(10)
        self.textview.set_margin_start(5)
        self.textview.set_margin_end(5)
        self.textview.modify_font(self.font_desc)
        self.textview.set_size_request(1, 1)
        # Wrap TextView in a ScrolledWindow with scrollbars disabled.
        # This is the canonical GTK3 fix for word-wrap in an Overlay:
        # ScrolledWindow properly constrains its child's allocated width
        # to its own width, giving TextView a concrete measure to wrap
        # against. Without this, Overlay asks the TextView its preferred
        # width (infinite for unwrapped text) and allocates that, so
        # long words never wrap and run off the edge of the window.
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        self.scroll.set_size_request(1, 1)
        self.scroll.set_hexpand(True)
        self.scroll.set_vexpand(True)
        self.scroll.set_halign(Gtk.Align.FILL)
        self.scroll.set_valign(Gtk.Align.FILL)
        self.scroll.add(self.textview)
        overlay.add_overlay(self.scroll)

        self.textview.get_buffer().connect("changed", self.on_text_changed)

        self.close_button = Gtk.Button()
        self.close_button.set_name("close-button")
        self.close_button.set_relief(Gtk.ReliefStyle.NONE)
        self.close_button.set_focus_on_click(False)
        self.update_close_button()
        self.close_button.connect("clicked", self.on_close)
        self.close_button.set_halign(Gtk.Align.START)
        self.close_button.set_valign(Gtk.Align.START)
        self.close_button.set_margin_start(5)
        self.close_button.set_margin_top(2)
        overlay.add_overlay(self.close_button)
        self.close_button.hide()

        # Resize grip — invisible but still interactive (14x14 hit area)
        grip = Gtk.EventBox()  # EventBox gives us a clickable area with no visual
        grip.set_size_request(14, 14)
        grip.set_halign(Gtk.Align.END)
        grip.set_valign(Gtk.Align.END)
        grip.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        # Make it completely invisible
        grip.set_visible_window(False)
        
        def _grip_press(w, event):
            if event.button == 1:
                self.begin_resize_drag(
                    Gdk.WindowEdge.SOUTH_EAST, 1,
                    int(event.x_root), int(event.y_root), event.time)
        grip.connect('button-press-event', _grip_press)
        overlay.add_overlay(grip)
        vbox.pack_start(overlay, True, True, 0)
        self.set_default_size(102, 62)

        if not start_active:
            self.pixbufs = self.pixbufs_inactive
            self.nine_slice.set_pixbufs(self.pixbufs)
            self.close_button.hide()
        else:
            # Defer focus grab until the window is fully realized and mapped.
            # present() raises the window and gives it WM focus; then
            # grab_focus() routes keyboard input straight into the TextView.
            def _grab():
                self.present()
                self.textview.grab_focus()
                return False
            GLib.idle_add(_grab)

    def serialize(self):
        buf = self.textview.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, True)
        x, y = self.get_position()
        w, h = self.get_size()
        return {
            "text": text,
            "x": x, "y": y,
            "width": w, "height": h,
            "color": self.color,
            "font": self.font_desc.to_string(),
            "creation_date": self.creation_date.isoformat(),
            "modified_date": self.modified_date.isoformat()
        }

    def load_textures(self):
        color_dir = os.path.join(BASE_DIR, "colors", self.color)
        self.pixbufs_active   = self.load_pixbufs(color_dir, "")
        self.pixbufs_inactive = self.load_pixbufs(color_dir, "_inactive")
        self.pixbufs = self.pixbufs_active

    def load_pixbufs(self, base_dir, suffix):
        return {
            "tl": GdkPixbuf.Pixbuf.new_from_file(os.path.join(base_dir, f"tl{suffix}.png")),
            "t":  GdkPixbuf.Pixbuf.new_from_file(os.path.join(base_dir, f"t{suffix}.png")),
            "tr": GdkPixbuf.Pixbuf.new_from_file(os.path.join(base_dir, f"tr{suffix}.png")),
            "l":  GdkPixbuf.Pixbuf.new_from_file(os.path.join(base_dir, f"l{suffix}.png")),
            "center": GdkPixbuf.Pixbuf.new_from_file(os.path.join(base_dir, f"center{suffix}.png")),
            "r":  GdkPixbuf.Pixbuf.new_from_file(os.path.join(base_dir, f"r{suffix}.png")),
            "bl": GdkPixbuf.Pixbuf.new_from_file(os.path.join(base_dir, f"bl{suffix}.png")),
            "b":  GdkPixbuf.Pixbuf.new_from_file(os.path.join(base_dir, f"b{suffix}.png")),
            "br": GdkPixbuf.Pixbuf.new_from_file(os.path.join(base_dir, f"br{suffix}.png")),
        }

    def update_close_button(self):
        close_path = os.path.join(BASE_DIR, "colors", self.color, "close_texture1.png")
        if os.path.exists(close_path):
            close_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(close_path, 7, 7)
            self.close_button.set_image(Gtk.Image.new_from_pixbuf(close_pixbuf))

    def create_menubar(self):
        menubar = Gtk.MenuBar()
        filemenu = Gtk.Menu()
        file_item = Gtk.MenuItem(label="File")
        file_item.set_submenu(filemenu)
        new_item = Gtk.MenuItem(label="New Note")
        new_item.connect("activate", self.on_new_note)
        filemenu.append(new_item)
        save_item = Gtk.MenuItem(label="Save As...")
        save_item.connect("activate", self.on_save_as)
        filemenu.append(save_item)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self.on_quit)
        filemenu.append(quit_item)
        editmenu = Gtk.Menu()
        edit_item = Gtk.MenuItem(label="Edit")
        edit_item.set_submenu(editmenu)
        copy_item = Gtk.MenuItem(label="Copy")
        copy_item.connect("activate", self.on_copy)
        editmenu.append(copy_item)
        paste_item = Gtk.MenuItem(label="Paste")
        paste_item.connect("activate", self.on_paste)
        editmenu.append(paste_item)
        notemenu = Gtk.Menu()
        note_item = Gtk.MenuItem(label="Note")
        note_item.set_submenu(notemenu)
        style_item = Gtk.MenuItem(label="Text Style")
        style_item.connect("activate", self.on_text_style)
        notemenu.append(style_item)
        info_item = Gtk.MenuItem(label="Note Info")
        info_item.connect("activate", self.on_note_info)
        notemenu.append(info_item)
        colormenu = Gtk.Menu()
        color_item = Gtk.MenuItem(label="Color")
        color_item.set_submenu(colormenu)
        colors = ["yellow", "blue", "green", "pink", "purple", "gray", "bw"]
        labels = ["Yellow", "Blue", "Green", "Pink", "Purple", "Gray", "Black & White"]
        for c, label in zip(colors, labels):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", self.on_change_color, c)
            colormenu.append(mi)
        helpmenu = Gtk.Menu()
        help_item = Gtk.MenuItem(label="Help")
        help_item.set_submenu(helpmenu)
        about_item = Gtk.MenuItem(label="About")
        about_item.connect("activate", self.on_about)
        helpmenu.append(about_item)
        menubar.append(file_item)
        menubar.append(edit_item)
        menubar.append(note_item)
        menubar.append(color_item)
        menubar.append(help_item)
        return menubar

    def on_new_note(self, widget):
        settings = load_settings()
        note = StickyNote(
            color=settings.get("last_color", "yellow"),
            font=settings.get("last_font", "Geneva-Ori 6")
        )
        note.show_all()
        app.notes.append(note)
        note.connect("destroy", lambda w, n=note: app.note_closed(n))

    def on_save_as(self, widget):
        dialog = Gtk.FileChooserDialog(
            title="Save Note As", parent=self,
            action=Gtk.FileChooserAction.SAVE,
            buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                     Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        )
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name("note.txt")
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            filename = dialog.get_filename()
            buf = self.textview.get_buffer()
            start, end = buf.get_bounds()
            text = buf.get_text(start, end, True)
            with open(filename, "w") as f:
                f.write(text)
        dialog.destroy()

    def on_copy(self, widget):
        self.textview.get_buffer().copy_clipboard(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD))

    def on_paste(self, widget):
        self.textview.get_buffer().paste_clipboard(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD), None, True)

    def on_text_style(self, widget):
        dialog = Gtk.FontChooserDialog("Select Font", self)
        dialog.set_font_desc(self.font_desc)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            self.font_desc = dialog.get_font_desc()
            self.textview.modify_font(self.font_desc)
        dialog.destroy()

    def on_note_info(self, widget):
        created_str  = self.creation_date.strftime("%a, %b %d, %Y, %I:%M %p")
        modified_str = self.modified_date.strftime("%a, %b %d, %Y, %I:%M %p")
        md = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
        )
        md.set_title("Note Info")
        md.format_secondary_text(f"Created:       {created_str}\nLast Modified:  {modified_str}")
        icon_path = os.path.join(BASE_DIR, "when.png")
        if os.path.exists(icon_path):
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(icon_path, 32, 32)
            md.set_image(Gtk.Image.new_from_pixbuf(pixbuf))
        md.run()
        md.destroy()

    def on_change_color(self, widget, color):
        self.color = color
        self.load_textures()
        self.update_close_button()
        self.nine_slice.set_pixbufs(self.pixbufs)
        settings = load_settings()
        settings["last_color"] = color
        save_settings(settings)

    def on_about(self, widget):
        about = Gtk.AboutDialog(transient_for=self, modal=True)
        about.set_program_name("Stickies")
        about.set_version("2.1")
        about.set_comments("Recreation of Stickies from Mac OS 9")
        about.run()
        about.destroy()

    def on_text_changed(self, buffer):
        self.modified_date = datetime.datetime.now()

    def on_focus_in(self, *args):
        self.pixbufs = self.pixbufs_active
        self.nine_slice.set_pixbufs(self.pixbufs)
        self.close_button.show()

    def on_focus_out(self, *args):
        self.pixbufs = self.pixbufs_inactive
        self.nine_slice.set_pixbufs(self.pixbufs)
        self.close_button.hide()

    def on_button_press(self, widget, event):
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 1:
            w, h = self.get_allocation().width, self.get_allocation().height
            x, y = event.x, event.y
            border = 8
            titlebar_height = 30
            move_zone_width = 120

            if y < border and x < border:
                self.begin_resize_drag(Gdk.WindowEdge.NORTH_WEST, 1, int(event.x_root), int(event.y_root), event.time)
            elif y < border and x > w - border:
                self.begin_resize_drag(Gdk.WindowEdge.NORTH_EAST, 1, int(event.x_root), int(event.y_root), event.time)
            elif y > h - border and x < border:
                self.begin_resize_drag(Gdk.WindowEdge.SOUTH_WEST, 1, int(event.x_root), int(event.y_root), event.time)
            elif y > h - border and x > w - border:
                self.begin_resize_drag(Gdk.WindowEdge.SOUTH_EAST, 1, int(event.x_root), int(event.y_root), event.time)
            elif y > h - border:
                self.begin_resize_drag(Gdk.WindowEdge.SOUTH, 1, int(event.x_root), int(event.y_root), event.time)
            elif x < border:
                self.begin_resize_drag(Gdk.WindowEdge.WEST, 1, int(event.x_root), int(event.y_root), event.time)
            elif x > w - border:
                self.begin_resize_drag(Gdk.WindowEdge.EAST, 1, int(event.x_root), int(event.y_root), event.time)

            move_left  = (w - move_zone_width) // 2
            move_right = move_left + move_zone_width
            if y < titlebar_height and move_left <= x <= move_right:
                self.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)

    def on_close(self, button):
        self.destroy()

    def on_quit(self, widget):
        app.save_and_quit()


class StickyApp:
    def __init__(self):
        self.notes = []
        self.settings = load_settings()

        self.parent_window = Gtk.Window()
        self.parent_window.hide()

        self.server = None
        self.server_thread = None
        self._start_socket_server()

        for note_data in self.settings.get("notes", []):
            note = StickyNote(color=note_data.get("color"), font=note_data.get("font"), start_active=False)
            note.show_all()
            buf = note.textview.get_buffer()
            buf.set_text(note_data.get("text", ""))
            note.move(note_data.get("x", 100), note_data.get("y", 100))
            note.resize(note_data.get("width", 102), note_data.get("height", 62))
            if "creation_date" in note_data:
                note.creation_date = datetime.datetime.fromisoformat(note_data["creation_date"])
            if "modified_date" in note_data:
                note.modified_date = datetime.datetime.fromisoformat(note_data["modified_date"])
            self.notes.append(note)
            note.connect("destroy", lambda w, n=note: self.note_closed(n))

        if not self.notes:
            n = StickyNote()
            n.show_all()
            self.notes.append(n)
            n.connect("destroy", lambda w, n=n: self.note_closed(n))

    def note_closed(self, note):
        try:
            if note in self.notes:
                self.notes.remove(note)
        except Exception:
            traceback.print_exc()
        self.save_settings_now()
        if not any(win.is_visible() for win in self.notes):
            self.save_and_quit()

    def save_settings_now(self):
        data = {"notes": [note.serialize() for note in self.notes if note.is_visible()]}
        save_settings(data)

    def save_and_quit(self):
        try:
            data = {"notes": [note.serialize() for note in self.notes if note.is_visible()]}
            save_settings(data)
        except Exception:
            traceback.print_exc()
        self._stop_socket_server()
        Gtk.main_quit()

    def _start_socket_server(self):
        try:
            if os.path.exists(SOCKET_PATH):
                try:
                    scheck = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    scheck.settimeout(0.1)
                    scheck.connect(SOCKET_PATH)
                    try:
                        scheck.sendall(b"NEW\n")
                    except Exception:
                        pass
                    scheck.close()
                    raise SystemExit(0)
                except (socket.error, SystemExit):
                    try:
                        os.unlink(SOCKET_PATH)
                    except Exception:
                        pass

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(SOCKET_PATH)
            server.listen(1)
            self.server = server

            def server_loop():
                while True:
                    try:
                        conn, _ = server.accept()
                        data = b""
                        try:
                            conn.settimeout(1.0)
                            data = conn.recv(1024)
                        except Exception:
                            pass
                        finally:
                            try:
                                conn.close()
                            except Exception:
                                pass
                        if not data:
                            continue
                        msg = data.decode(errors="ignore").strip().upper()
                        if msg == "NEW":
                            GLib.idle_add(self._create_new_note_from_signal)
                    except Exception:
                        break

            self.server_thread = threading.Thread(target=server_loop, daemon=True)
            self.server_thread.start()
        except SystemExit:
            raise
        except Exception:
            traceback.print_exc()
            self.server = None

    def _create_new_note_from_signal(self):
        settings = load_settings()
        note = StickyNote(
            color=settings.get("last_color", "yellow"),
            font=settings.get("last_font", "Geneva-Ori 6")
        )
        note.show_all()
        self.notes.append(note)
        note.connect("destroy", lambda w, n=note: self.note_closed(n))
        return False

    def _stop_socket_server(self):
        try:
            if self.server:
                try:
                    self.server.close()
                except Exception:
                    pass
                self.server = None
            if os.path.exists(SOCKET_PATH):
                try:
                    os.unlink(SOCKET_PATH)
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    try:
        app = StickyApp()
    except SystemExit:
        raise SystemExit(0)
    Gtk.main()
