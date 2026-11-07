"""The "AI agent" window: choose which AI answers questions about your meetings, connect it, test it."""
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

import agentkeys  # noqa: E402
import agents  # noqa: E402
import chat  # noqa: E402

NEEDS_ADDRESS = {"custom", "ollama", "lmstudio"}


class AgentDialog(Adw.Dialog):
    def __init__(self, on_saved, reason=None):
        super().__init__(title="AI agent", content_width=580, content_height=720)
        self.on_saved = on_saved
        self.saved = agents.load_config()
        self.busy = 0

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

        if reason:
            note = Adw.PreferencesGroup()
            row = Adw.ActionRow(title=reason, subtitle="Choose an AI agent below to ask questions about your meetings.",
                                use_markup=False)
            row.set_title_lines(3)
            icon = Gtk.Image(icon_name="dialog-information-symbolic")
            icon.add_css_class("accent")
            row.add_prefix(icon)
            note.add(row)
            page.add(note)

        pick = Adw.PreferencesGroup(
            title="Which AI answers your questions?",
            description="When you ask about a meeting, MintoMeet sends that meeting's transcript text (never the "
                        "audio) to the agent you choose. Agents on this computer keep everything on it.")
        self.service_ids = [s[0] for s in agents.SERVICES]
        self.service_row = Adw.ComboRow(title="Service", model=Gtk.StringList.new([s[1] for s in agents.SERVICES]),
                                        use_subtitle=True)    # long names in full, not cut off at the side
        pick.add(self.service_row)
        self.about_row = Adw.ActionRow(use_markup=False)        # what the chosen service is, in a sentence
        self.about_row.set_title_lines(3)
        self.about_row.add_css_class("dim-label")
        self.about_row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        pick.add(self.about_row)
        page.add(pick)

        conn = Adw.PreferencesGroup(title="Connection")
        self.url_row = Adw.EntryRow(title="Server address")
        self.key_row = Adw.PasswordEntryRow(title="API key")
        self.key_link = Gtk.Button(icon_name="web-browser-symbolic", valign=Gtk.Align.CENTER,
                                   tooltip_text="Get an API key (opens your browser)")
        self.key_link.add_css_class("flat")
        self.key_link.connect("clicked", lambda _b: self.open_link())
        self.key_row.add_suffix(self.key_link)
        self.model_row = Adw.EntryRow(title="Model")
        self.models_popover = Gtk.Popover()
        self.models_btn = Gtk.MenuButton(icon_name="view-list-symbolic", valign=Gtk.Align.CENTER,
                                         popover=self.models_popover, tooltip_text="Choose from the available models")
        self.models_btn.add_css_class("flat")
        self.models_btn.set_create_popup_func(lambda _b: self.fill_models())
        self.model_row.add_suffix(self.models_btn)
        for row in (self.url_row, self.key_row, self.model_row):
            conn.add(row)
            row.connect("changed", lambda _r: self.clear_result())
        self.link_row = Adw.ActionRow(title="Get the app", activatable=True)
        self.link_row.add_suffix(Gtk.Image(icon_name="web-browser-symbolic"))
        self.link_row.connect("activated", lambda _r: self.open_link())
        conn.add(self.link_row)
        page.add(conn)

        status = Adw.PreferencesGroup()
        self.privacy_row = Adw.ActionRow(use_markup=False)
        self.privacy_row.set_title_lines(3)
        self.privacy_icon = Gtk.Image()
        self.privacy_row.add_prefix(self.privacy_icon)
        status.add(self.privacy_row)
        self.result_row = Adw.ActionRow(visible=False, use_markup=False)
        self.result_row.set_title_lines(4)
        self.result_icon = Gtk.Image()
        self.result_row.add_prefix(self.result_icon)
        status.add(self.result_row)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=12)
        self.spinner = Adw.Spinner(visible=False)
        self.test_btn = Gtk.Button(label="Test connection")
        self.test_btn.connect("clicked", lambda _b: self.test())
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", lambda _b: self.save())
        for w in (self.spinner, self.test_btn, self.save_btn):
            buttons.append(w)
        status.add(buttons)
        page.add(status)

        self.service_row.set_selected(self.service_ids.index(self.saved["service"]))
        self.service_row.connect("notify::selected", lambda *_a: self.on_service())
        self.on_service(initial=True)

    # --- form ------------------------------------------------------------------------------------------------
    def service_id(self):
        return self.service_ids[self.service_row.get_selected()]

    def current(self):
        sid = self.service_id()
        url = self.url_row.get_text().strip() if sid in NEEDS_ADDRESS else ""
        if url == agents.SERVICE[sid]["base_url"]:
            url = ""
        return {"service": sid, "base_url": url, "model": self.model_row.get_text().strip()}

    def key(self):
        return self.key_row.get_text().strip()

    def on_service(self, initial=False):
        sid = self.service_id()
        s = agents.SERVICE[sid]
        same = sid == self.saved["service"]
        self.about_row.set_title(s["about"])
        self.url_row.set_visible(sid in NEEDS_ADDRESS)
        self.url_row.set_text((self.saved["base_url"] if same else "") or s["base_url"])
        self.key_row.set_visible(s["key"] != "no")
        self.key_row.set_title("API key" + (" (if your server needs one)" if s["key"] == "optional" else ""))
        self.key_link.set_visible(bool(s["key_url"]) and s["key"] == "yes")
        cfg = {**self.current(), "service": sid}
        self.key_row.set_text(agentkeys.get(agents.account(cfg)) if s["key"] != "no" else "")
        self.model_row.set_text(self.saved["model"] if same else "")
        self.model_row.set_title("Model (optional)" if s["kind"] == "claude-code" else "Model")
        app_link = s["key"] == "no" and s["key_url"]
        self.link_row.set_visible(bool(app_link))
        if app_link:
            self.link_row.set_title(f"Get {s['label'].split(' (')[0]}")
            self.link_row.set_subtitle(s["key_url"])
        self.clear_result()
        self.update_privacy()

    def update_privacy(self):
        cfg = self.current()
        local = agents.is_local(cfg)
        self.privacy_row.set_title(agents.usage_note(cfg))
        self.privacy_icon.set_from_icon_name("computer-symbolic" if local else "network-transmit-receive-symbolic")

    def open_link(self):
        url = agents.SERVICE[self.service_id()]["key_url"]
        if url:
            Gio.AppInfo.launch_default_for_uri(url, None)

    def clear_result(self):
        self.result_row.set_visible(False)
        if hasattr(self, "privacy_row"):
            self.update_privacy()

    def show_result(self, ok, text):
        self.result_row.set_title(text)
        self.result_icon.set_from_icon_name("object-select-symbolic" if ok else "dialog-warning-symbolic")
        for c in ("success", "warning"):
            self.result_icon.remove_css_class(c)
        self.result_icon.add_css_class("success" if ok else "warning")
        self.result_row.set_visible(True)

    def set_busy(self, delta):
        self.busy += delta
        self.spinner.set_visible(self.busy > 0)
        self.test_btn.set_sensitive(self.busy == 0)

    def in_background(self, work, done):
        """Run work() on a thread; done(result, error_text) back on the main loop."""
        self.set_busy(1)

        def run():
            try:
                result, error = work(), None
            except agents.AgentError as e:
                result, error = None, str(e)
            except Exception as e:  # noqa: BLE001
                result, error = None, f"Unexpected problem: {e}"
            GLib.idle_add(lambda: (self.set_busy(-1), done(result, error), False)[2])
        threading.Thread(target=run, daemon=True).start()

    # --- models ----------------------------------------------------------------------------------------------
    def fill_models(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        box.append(Adw.Spinner(halign=Gtk.Align.CENTER, margin_top=8, margin_bottom=8))
        self.models_popover.set_child(box)
        cfg, key = self.current(), self.key()

        def done(models, error):
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            if error or not models:
                label = Gtk.Label(label=error or "This service didn't list any models. Type the model name instead.",
                                  wrap=True, max_width_chars=40, xalign=0, margin_top=6, margin_bottom=6,
                                  margin_start=6, margin_end=6)
                label.add_css_class("dim-label")
                inner.append(label)
            for name in models or []:
                b = Gtk.Button(label=name)
                b.add_css_class("flat")
                b.get_child().set_xalign(0)
                b.connect("clicked", lambda _b, n=name: (self.model_row.set_text(n), self.models_popover.popdown()))
                inner.append(b)
            self.models_popover.set_child(Gtk.ScrolledWindow(child=inner, propagate_natural_height=True,
                                                             max_content_height=360, min_content_width=280,
                                                             hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.in_background(lambda: agents.list_models(cfg, key), done)

    # --- test and save ---------------------------------------------------------------------------------------
    def test(self):
        cfg, key = self.current(), self.key()
        if agents.kind(cfg) == "claude-code":
            work = chat.check_claude_code
        else:
            issue = agents.problem(cfg, key)
            if issue:
                self.show_result(False, issue)
                return
            work = lambda: agents.test_connection(cfg, key)  # noqa: E731
        self.show_result(True, "Testing…")
        self.in_background(work, lambda text, error: self.show_result(error is None, error or text))

    def save(self):
        cfg, key = self.current(), self.key()
        issue = agents.problem(cfg, key) if agents.kind(cfg) != "claude-code" else None
        if issue:
            self.show_result(False, issue)
            return
        if agents.SERVICE[cfg["service"]]["key"] != "no":
            agentkeys.put(agents.account(cfg), key)
        saved = agents.save_config(cfg)
        self.close()
        self.on_saved(saved)
