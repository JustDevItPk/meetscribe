"""The "Speech models" library: what this computer can run, each model's requirements, and downloading or
removing models. Downloads run in MintoMeet's own Python (`worker.py --download <id>`), one at a time, and
keep going when the dialog is closed."""
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import datasize  # noqa: E402
import modelcatalog  # noqa: E402

GB = modelcatalog.GB
LEVEL_ICONS = {"great": ("object-select-symbolic", "success"), "ok": ("dialog-information-symbolic", "accent"),
               "slow": ("dialog-warning-symbolic", "warning"), "no": ("action-unavailable-symbolic", "error")}


def size_text(mb):
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"


class Downloads:
    """At most one model download at a time, shared by every open library dialog."""

    def __init__(self, python, worker, on_done):
        self.python, self.worker, self.on_done = python, worker, on_done
        self.model_id = None        # being downloaded
        self.pct = 0
        self.proc = None
        self.cancelled = False
        self.listeners = []         # called with no arguments whenever something changes

    @property
    def busy(self):
        return self.model_id is not None

    def notify(self):
        for listener in list(self.listeners):
            listener()
        return False

    def start(self, model_id):
        if self.busy:
            return False
        try:
            self.proc = subprocess.Popen([str(self.python), str(self.worker), "--download", model_id],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True)
        except OSError as e:
            self.on_done(model_id, False, f"Couldn't start the download: {e}")
            return False
        self.model_id, self.pct, self.cancelled = model_id, 0, False
        threading.Thread(target=self._read, args=(self.proc, model_id), daemon=True).start()
        self.notify()
        return True

    def _read(self, proc, model_id):
        error = None
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("STATE: downloading pct="):
                try:
                    self.pct = int(line.rsplit("=", 1)[1])
                except ValueError:
                    continue
                GLib.idle_add(self.notify)
            elif line.startswith("ERROR: "):
                error = line[7:]
        code = proc.wait()
        if not error and code != 0 and not self.cancelled:
            tail = proc.stderr.read().strip().splitlines()
            error = f"The download stopped unexpectedly. {tail[-1] if tail else ''}".strip()
        GLib.idle_add(self._finished, model_id, code == 0 and not self.cancelled, error)

    def _finished(self, model_id, ok, error):
        cancelled = self.cancelled
        self.model_id, self.proc, self.pct = None, None, 0
        if not ok:
            threading.Thread(target=self._clean_up, args=(model_id,), daemon=True).start()
        datasize.invalidate()
        if not cancelled:
            self.on_done(model_id, ok, error)
        self.notify()
        return False

    @staticmethod
    def _clean_up(model_id):
        if not modelcatalog.is_downloaded(modelcatalog.get(model_id)):
            try:
                modelcatalog.delete(model_id)        # half-finished files only take space
            except (OSError, ValueError):
                pass

    def cancel(self):
        if self.proc is not None and self.proc.poll() is None:
            self.cancelled = True
            self.proc.terminate()


class ModelLibraryDialog(Adw.Dialog):
    """Your computer's specs, then every model with its requirements and a Download / Cancel / Delete button."""

    def __init__(self, downloads, in_use, can_delete, on_changed):
        super().__init__(title="Speech models", content_width=640, content_height=760)
        self.downloads, self.in_use, self.can_delete, self.on_changed = downloads, in_use, can_delete, on_changed
        self.progress = {}      # model id -> progress bar while it downloads
        self.open_rows = set()  # expanded rows survive a redraw
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.view = view
        self.toasts.set_child(view)
        self.set_child(self.toasts)
        self.downloads.listeners.append(self.on_downloads_changed)
        self.connect("closed", lambda _d: self.downloads.listeners.remove(self.on_downloads_changed))
        self.shown_state = None
        self.fill()

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=6, use_markup=False))

    def on_downloads_changed(self):
        state = (self.downloads.model_id, self.downloads.busy)
        bar = self.progress.get(self.downloads.model_id)
        if state == self.shown_state and bar is not None:
            bar.set_fraction(self.downloads.pct / 100)
            bar.set_text(f"{self.downloads.pct}%")
        else:
            self.fill()         # a download started or finished: redraw the buttons

    # --- layout -------------------------------------------------------------------
    def fill(self):
        self.specs = modelcatalog.detect()
        self.shown_state = (self.downloads.model_id, self.downloads.busy)
        self.progress = {}
        specs = self.specs
        page = Adw.PreferencesPage()

        computer = Adw.PreferencesGroup(
            title="This computer",
            description="MintoMeet transcribes on your processor; no graphics card is needed. The advice below is an "
                        "estimate for this computer.")
        for title, value in (
                ("Processor", f"{specs.cpu} · {specs.cores} cores ({specs.threads} threads)"),
                ("Memory", f"{specs.ram_total / GB:.1f} GB ({specs.ram_available / GB:.1f} GB free right now)"),
                ("Free disk space", f"{specs.disk_free / GB:.0f} GB where models are saved")):
            row = Adw.ActionRow(title=title, subtitle=value, use_markup=False)
            row.add_css_class("property")
            computer.add(row)
        page.add(computer)

        badges = modelcatalog.suggestions(specs)
        have = [m for m in modelcatalog.MODELS if modelcatalog.is_downloaded(m)]
        rest = [m for m in modelcatalog.MODELS if m not in have]
        if have:
            group = Adw.PreferencesGroup(title="On this computer",
                                         description="Pick one in Recording settings before you start a meeting.")
            for m in have:
                group.add(self.model_row(m, True, badges.get(m.id)))
            page.add(group)
        if rest:
            group = Adw.PreferencesGroup(title="Available to download",
                                         description="Downloaded once, then used without the internet. Open a model to "
                                                     "see what it needs.")
            for m in rest:
                group.add(self.model_row(m, False, badges.get(m.id)))
            page.add(group)
        self.view.set_content(page)

    def model_row(self, m, downloaded, badge):
        f = modelcatalog.fit(m, self.specs, downloaded)
        langs = "English only" if m.languages == "English only" else (
            "~100 languages" if m.multilingual else "English + 24 European languages")
        row = Adw.ExpanderRow(title=m.name, use_markup=False,
                              subtitle=f"{langs} · {size_text(m.download_mb)} · {f.label}")
        row.set_subtitle_lines(2)
        row.set_expanded(m.id in self.open_rows)
        row.connect("notify::expanded", lambda r, _p: (self.open_rows.add(m.id) if r.get_expanded()
                                                        else self.open_rows.discard(m.id)))
        icon_name, css = LEVEL_ICONS[f.level]
        icon = Gtk.Image(icon_name=icon_name, tooltip_text=f.reason)
        icon.add_css_class(css)
        row.add_prefix(icon)
        if badge:
            short, why = badge
            pill = Gtk.Label(label=short, valign=Gtk.Align.CENTER, tooltip_text=why)
            pill.add_css_class("model-badge")
            row.add_suffix(pill)
        row.add_suffix(self.action(m, downloaded, f))

        threads = f"Uses up to {modelcatalog.live_threads(m, self.specs)} processor cores while recording"
        on_disk = modelcatalog.size_on_disk(m) if downloaded else 0
        details = [
            ("How well it runs here", f.reason),
            ("Languages", m.languages),
            ("Accuracy", m.accuracy),
            ("Memory needed", f"About {m.ram_gb:g} GB while transcribing (this computer has "
                              f"{self.specs.ram_total / GB:.0f} GB)"),
            ("Processor", threads + ("" if m.measured else ". Speed estimated from the model's size")),
            ("Download size", size_text(m.download_mb) + (f" · {datasize.fmt(on_disk)} on disk" if on_disk else "")),
            ("Made by", f"{m.by} · {m.license} license"),
        ]
        for title, value in details:
            d = Adw.ActionRow(title=title, subtitle=value, use_markup=False)
            d.set_subtitle_lines(0)
            d.add_css_class("property")
            row.add_row(d)
        return row

    def action(self, m, downloaded, f):
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        dl = self.downloads
        if dl.model_id == m.id:
            bar = Gtk.ProgressBar(fraction=dl.pct / 100, show_text=True, text=f"{dl.pct}%", width_request=110,
                                  valign=Gtk.Align.CENTER)
            self.progress[m.id] = bar
            cancel = Gtk.Button(label="Cancel")
            cancel.set_tooltip_text("Stop the download and remove what was downloaded so far")
            cancel.connect("clicked", lambda _b: dl.cancel())
            box.append(bar)
            box.append(cancel)
        elif downloaded:
            why = self.in_use(m.id)
            if why:
                label = Gtk.Label(label="In use", tooltip_text=why)
                label.add_css_class("dim-label")
                box.append(label)
            else:
                delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=f"Delete {m.name} from this computer")
                delete.add_css_class("flat")
                delete.update_property([Gtk.AccessibleProperty.LABEL], [f"Delete {m.name}"])
                delete.connect("clicked", lambda _b: self.confirm_delete(m))
                box.append(delete)
        else:
            get = Gtk.Button(label="Download")
            if f.level == "no":
                get.set_sensitive(False)
                get.set_tooltip_text(f.reason)
            elif dl.busy:
                get.set_sensitive(False)
                get.set_tooltip_text("Another model is downloading. Models download one at a time.")
            else:
                get.add_css_class("suggested-action" if f.level in ("great", "ok") else "flat")
                get.set_tooltip_text(f"Download {size_text(m.download_mb)}")
            get.connect("clicked", lambda _b: self.download(m, f))
            box.append(get)
        return box

    # --- actions ------------------------------------------------------------------
    def download(self, m, f):
        if f.level == "slow":
            self.toast(f"{m.name} is too slow to keep up live on this computer, but works for “Transcribe again”.")
        self.open_rows.discard(m.id)
        self.downloads.start(m.id)

    def confirm_delete(self, m):
        problem = self.can_delete(m.id)
        if problem:
            self.toast(problem)
            return
        size = modelcatalog.size_on_disk(m)
        dialog = Adw.AlertDialog(heading=f"Delete {m.name}?",
                                 body=f"This frees {datasize.fmt(size)}. Your transcripts stay as they are, and you can "
                                      "download the model again any time.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: r == "delete" and self.delete(m))
        dialog.present(self)

    def delete(self, m):
        try:
            freed = modelcatalog.delete(m.id)
        except (OSError, ValueError) as e:
            self.toast(f"Couldn't delete {m.name}: {e}")
            return
        datasize.invalidate()
        self.toast(f"Deleted {m.name}, freeing {datasize.fmt(freed)}.")
        self.on_changed()
        self.fill()
