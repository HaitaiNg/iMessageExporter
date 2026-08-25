"""
gui_app.py — Desktop GUI for imessage-export.

For people who don't want to touch a terminal: pick a chat from a list,
pick what to export, pick where to save it, click a button. Built with
Tkinter (ships with Python) so it can be packaged into a double-clickable
.app with py2app without pulling in a heavier UI toolkit.

All actual work (reading the DB, writing files) is delegated to
messages_library / ios_backup / exporters — this module is presentation and
event wiring only.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from exporters import export_attachments, export_pdf, export_text
from ios_backup import Backup, extract_sms_db, find_backup, list_backups
from messages_library import Chat, connect, list_chats, snapshot_db

# Tkinter is not thread-safe — its macOS/Cocoa backend in particular can
# silently drop calls made from a non-main thread (including .after()),
# which looks exactly like "the window opens but nothing ever happens."
# Background work must never touch Tk directly: it only puts a zero-arg
# callback on this queue, which the main thread drains via .after() polling.

FORMAT_TEXT = "Text (.txt)"
FORMAT_ATTACHMENTS = "Attachments (folder)"
FORMAT_PDF = "PDF (text + photos)"
FORMATS = [FORMAT_TEXT, FORMAT_ATTACHMENTS, FORMAT_PDF]

# Color palette + fonts for the custom "clam"-based look below. Kept as
# module constants (rather than buried in _setup_style) so the whole visual
# identity is readable in one place.
BG_MAIN = "#f5f5f7"
BG_CARD = "#ffffff"
BORDER = "#dcdce1"
ACCENT = "#0a84ff"
ACCENT_ACTIVE = "#0071e3"
TEXT_PRIMARY = "#1d1d1f"
TEXT_SECONDARY = "#6e6e73"
ROW_ALT = "#f2f2f5"
STATUS_COLORS = {"info": TEXT_SECONDARY, "success": "#1a7f37", "error": "#d70015"}

FONT_HEADER = ("SF Pro Display", 19, "bold")
FONT_SUBTITLE = ("SF Pro Text", 12)
FONT_LABEL = ("SF Pro Text", 12)
FONT_BODY = ("SF Pro Text", 12)
FONT_LIST = ("SF Pro Text", 13)
FONT_BUTTON = ("SF Pro Text", 12, "bold")


class ExportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("iMessage Export")
        self.geometry("600x520")
        self.minsize(480, 420)
        self.configure(bg=BG_MAIN)
        self._setup_style()

        # A sqlite3.Connection can only be used from the thread that created
        # it, but each background operation below runs on its own fresh
        # thread — so we keep the resolved db_path (cheap to reconnect from)
        # rather than a live connection, and each operation opens its own.
        self.db_path: str | None = None
        self.backup: Backup | None = None
        self.chats: list[Chat] = []

        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.after(50, self._poll_ui_queue)

        self._build_widgets()
        self._load_source(use_backup=None)

    def _poll_ui_queue(self) -> None:
        """Runs on the main thread only. Drains callbacks queued by
        background work and executes them here, where touching Tk is safe."""
        try:
            while True:
                callback = self._ui_queue.get_nowait()
                callback()
        except queue.Empty:
            pass
        self.after(50, self._poll_ui_queue)

    def _run_async(self, work: Callable[[], None], on_done: Callable[[object], None]) -> None:
        """Run `work()` on a background thread. `on_done` is called back on
        the main thread with either the return value or the raised
        exception — never call Tk methods from inside `work` itself."""

        def runner() -> None:
            try:
                result: object = work()
            except Exception as e:  # noqa: BLE001 - surfaced to the user, not swallowed
                # `e` is bound as a default arg (evaluated now) rather than
                # captured by the closure (which would see it late) — except
                # blocks auto-delete their `as e` name once the block exits,
                # so a plain `lambda: on_done(e)` would NameError when the
                # main thread runs it later.
                self._ui_queue.put(lambda e=e: on_done(e))
            else:
                self._ui_queue.put(lambda: on_done(result))

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------
    # Style
    # ------------------------------------------------------------------

    def _setup_style(self) -> None:
        """Switch to the 'clam' theme so colors/fonts below actually take
        effect — the default macOS 'aqua' theme renders native controls and
        mostly ignores ttk style overrides (backgrounds, custom button
        colors), which is why the stock look was flat and generic."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background=BG_MAIN)
        style.configure("Card.TFrame", background=BG_CARD)
        style.configure("TLabel", background=BG_MAIN, foreground=TEXT_PRIMARY, font=FONT_BODY)
        style.configure("Header.TLabel", background=BG_MAIN, foreground=TEXT_PRIMARY, font=FONT_HEADER)
        style.configure("Subtitle.TLabel", background=BG_MAIN, foreground=TEXT_SECONDARY, font=FONT_SUBTITLE)
        style.configure("Field.TLabel", background=BG_MAIN, foreground=TEXT_SECONDARY, font=FONT_LABEL)
        style.configure("Status.TLabel", background=BG_MAIN, font=FONT_SUBTITLE)

        style.configure(
            "TEntry", fieldbackground=BG_CARD, bordercolor=BORDER, lightcolor=BORDER,
            darkcolor=BORDER, padding=6, font=FONT_BODY,
        )
        style.configure(
            "TCombobox", fieldbackground=BG_CARD, background=BG_CARD, bordercolor=BORDER,
            lightcolor=BORDER, darkcolor=BORDER, padding=5, arrowsize=12, font=FONT_BODY,
        )
        style.map("TCombobox", fieldbackground=[("readonly", BG_CARD)], foreground=[("readonly", TEXT_PRIMARY)])
        self.option_add("*TCombobox*Listbox.background", BG_CARD)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.font", FONT_BODY)

        style.configure(
            "TButton", background=BG_CARD, foreground=TEXT_PRIMARY, bordercolor=BORDER,
            lightcolor=BG_CARD, darkcolor=BG_CARD, focusthickness=0, padding=(10, 6), font=FONT_BODY,
        )
        style.map("TButton", background=[("active", ROW_ALT)])

        style.configure(
            "Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT,
            lightcolor=ACCENT, darkcolor=ACCENT, focusthickness=0, padding=(14, 7), font=FONT_BUTTON,
        )
        style.map(
            "Accent.TButton",
            background=[("disabled", "#b7d9fb"), ("active", ACCENT_ACTIVE)],
            foreground=[("disabled", "white")],
        )

        style.configure("Vertical.TScrollbar", background=BG_MAIN, troughcolor=BG_MAIN, bordercolor=BG_MAIN)
        style.configure("TSeparator", background=BORDER)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_widgets(self) -> None:
        container = ttk.Frame(self, padding=(20, 18))
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container)
        header.pack(fill="x")
        ttk.Label(header, text="iMessage Export", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header, text="Pick a chat, pick a format, export it.", style="Subtitle.TLabel"
        ).pack(anchor="w", pady=(2, 16))

        source_frame = ttk.Frame(container)
        source_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(source_frame, text="Source", style="Field.TLabel").pack(anchor="w", pady=(0, 4))
        source_row = ttk.Frame(source_frame)
        source_row.pack(fill="x")
        self.source_var = tk.StringVar(value="Live Mac database")
        self.source_menu = ttk.Combobox(source_row, textvariable=self.source_var, state="readonly")
        self.source_menu.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.source_menu.bind("<<ComboboxSelected>>", self._on_source_selected)
        ttk.Button(source_row, text="Refresh", command=self._refresh_sources).pack(side="left")

        self._refresh_sources()

        search_frame = ttk.Frame(container)
        search_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(search_frame, text="Search", style="Field.TLabel").pack(anchor="w", pady=(0, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render_chat_list())
        ttk.Entry(search_frame, textvariable=self.filter_var).pack(fill="x")

        list_card = tk.Frame(container, bg=BORDER)
        list_card.pack(fill="both", expand=True, pady=(0, 14))
        list_inner = tk.Frame(list_card, bg=BG_CARD)
        list_inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.chat_listbox = tk.Listbox(
            list_inner,
            activestyle="none",
            bg=BG_CARD,
            fg=TEXT_PRIMARY,
            font=FONT_LIST,
            bd=0,
            highlightthickness=0,
            selectbackground=ACCENT,
            selectforeground="white",
            selectborderwidth=0,
        )
        self.chat_listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        scrollbar = ttk.Scrollbar(list_inner, command=self.chat_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.chat_listbox.config(yscrollcommand=scrollbar.set)

        export_frame = ttk.Frame(container)
        export_frame.pack(fill="x")
        ttk.Label(export_frame, text="Export as", style="Field.TLabel").pack(anchor="w", pady=(0, 4))
        export_row = ttk.Frame(export_frame)
        export_row.pack(fill="x")
        self.format_var = tk.StringVar(value=FORMAT_TEXT)
        ttk.Combobox(
            export_row, textvariable=self.format_var, values=FORMATS, state="readonly"
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.export_button = ttk.Button(
            export_row, text="Export…", command=self._on_export_clicked, style="Accent.TButton"
        )
        self.export_button.pack(side="left")

        self.progress = ttk.Progressbar(container, mode="indeterminate")

        self._separator = ttk.Separator(container)
        self._separator.pack(fill="x", pady=(14, 8))
        self.status_var = tk.StringVar(value="Loading chats…")
        self.status_label = ttk.Label(container, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.pack(fill="x")
        self._set_status("Loading chats…", "info")

    def _set_status(self, message: str, kind: str = "info") -> None:
        self.status_var.set(message)
        self.status_label.configure(foreground=STATUS_COLORS[kind])

    def _show_progress(self) -> None:
        self.progress.pack(fill="x", pady=(0, 8), before=self._separator)
        self.progress.start(12)

    def _hide_progress(self) -> None:
        self.progress.stop()
        self.progress.pack_forget()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _refresh_sources(self) -> None:
        backups = list_backups()
        options = ["Live Mac database"]
        options += [f"iPhone backup: {b.device_name} ({b.udid[:8]}…)" for b in backups]
        self._backup_options = {opt: b for opt, b in zip(options[1:], backups)}
        self.source_menu["values"] = options
        if self.source_var.get() not in options:
            self.source_var.set(options[0])

    def _on_source_selected(self, _event=None) -> None:
        choice = self.source_var.get()
        backup = self._backup_options.get(choice)
        self._load_source(use_backup=backup)

    def _load_source(self, use_backup: Backup | None) -> None:
        self._set_status("Loading chats…", "info")
        self.export_button.state(["disabled"])
        self._show_progress()

        def work() -> tuple[str, list[Chat]]:
            if use_backup is not None:
                if use_backup.is_encrypted:
                    raise RuntimeError(
                        "That backup is encrypted, which isn't supported. "
                        "Re-create it in Finder with 'Encrypt local backup' unchecked."
                    )
                db_path = extract_sms_db(use_backup)
            else:
                db_path = snapshot_db()
            conn = connect(db_path)
            try:
                return db_path, list_chats(conn)
            finally:
                conn.close()

        def on_done(result: object) -> None:
            if isinstance(result, PermissionError):
                self._on_load_error(
                    "Can't read the Messages database — this app needs Full Disk Access.\n\n"
                    "Go to System Settings → Privacy & Security → Full Disk Access, "
                    "enable it for this app, then quit and reopen it."
                )
            elif isinstance(result, Exception):
                self._on_load_error(str(result))
            else:
                db_path, chats = result
                self._on_load_success(db_path, chats, use_backup)

        self._run_async(work, on_done)

    def _on_load_error(self, message: str) -> None:
        self._hide_progress()
        self._set_status("Couldn't load chats.", "error")
        messagebox.showerror("iMessage Export", message)

    def _on_load_success(self, db_path: str, chats: list[Chat], backup: Backup | None) -> None:
        self._hide_progress()
        self.db_path = db_path
        self.backup = backup
        self.chats = chats
        self.export_button.state(["!disabled"])
        self._set_status(f"{len(chats)} chats loaded.", "success")
        self._render_chat_list()

    def _render_chat_list(self) -> None:
        self.chat_listbox.delete(0, "end")
        needle = self.filter_var.get().strip().lower()
        self._visible_chats = [
            c for c in self.chats if not needle or needle in c.label.lower()
        ]
        for i, chat in enumerate(self._visible_chats):
            self.chat_listbox.insert("end", f"{chat.label}  ({chat.message_count} msgs)")
            if i % 2 == 1:
                self.chat_listbox.itemconfig(i, background=ROW_ALT)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _selected_chat(self) -> Chat | None:
        selection = self.chat_listbox.curselection()
        if not selection:
            return None
        return self._visible_chats[selection[0]]

    def _on_export_clicked(self) -> None:
        chat = self._selected_chat()
        if chat is None:
            messagebox.showinfo("iMessage Export", "Select a chat first.")
            return

        fmt = self.format_var.get()
        if fmt == FORMAT_ATTACHMENTS:
            output = filedialog.askdirectory(title="Choose a folder for the attachments")
        elif fmt == FORMAT_PDF:
            output = filedialog.asksaveasfilename(
                title="Save PDF as", defaultextension=".pdf", filetypes=[("PDF", "*.pdf")]
            )
        else:
            output = filedialog.asksaveasfilename(
                title="Save text as", defaultextension=".txt", filetypes=[("Text", "*.txt")]
            )
        if not output:
            return

        self.export_button.state(["disabled"])
        self._set_status(f"Exporting {chat.label}…", "info")
        self._show_progress()

        def work() -> str:
            conn = connect(self.db_path)
            try:
                if fmt == FORMAT_ATTACHMENTS:
                    result = export_attachments(conn, chat.chat_id, self.backup, output)
                    return f"Copied {result.copied} attachments ({result.skipped} skipped)."
                if fmt == FORMAT_PDF:
                    export_pdf(conn, chat.chat_id, self.backup, output, title=chat.label)
                    return f"Wrote {output}"
                count = export_text(conn, chat.chat_id, output)
                return f"Wrote {count} messages to {output}"
            finally:
                conn.close()

        def on_done(result: object) -> None:
            if isinstance(result, Exception):
                self._on_export_error(str(result))
            else:
                self._on_export_success(result)

        self._run_async(work, on_done)

    def _on_export_error(self, message: str) -> None:
        self._hide_progress()
        self.export_button.state(["!disabled"])
        self._set_status("Export failed.", "error")
        messagebox.showerror("iMessage Export", message)

    def _on_export_success(self, message: str) -> None:
        self._hide_progress()
        self.export_button.state(["!disabled"])
        self._set_status("Done.", "success")
        messagebox.showinfo("iMessage Export", message)


def main() -> None:
    app = ExportApp()
    app.mainloop()


if __name__ == "__main__":
    main()
