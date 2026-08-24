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


class ExportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("iMessage Export")
        self.geometry("560x480")
        self.minsize(480, 420)

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
    # Layout
    # ------------------------------------------------------------------

    def _build_widgets(self) -> None:
        pad = {"padx": 10, "pady": 6}

        source_frame = ttk.Frame(self)
        source_frame.pack(fill="x", **pad)

        ttk.Label(source_frame, text="Source:").pack(side="left")
        self.source_var = tk.StringVar(value="Live Mac database")
        self.source_menu = ttk.Combobox(source_frame, textvariable=self.source_var, state="readonly")
        self.source_menu.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.source_menu.bind("<<ComboboxSelected>>", self._on_source_selected)
        ttk.Button(source_frame, text="Refresh", command=self._refresh_sources).pack(side="left")

        self._refresh_sources()

        search_frame = ttk.Frame(self)
        search_frame.pack(fill="x", **pad)
        ttk.Label(search_frame, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render_chat_list())
        ttk.Entry(search_frame, textvariable=self.filter_var).pack(side="left", fill="x", expand=True, padx=(6, 0))

        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, **pad)
        self.chat_listbox = tk.Listbox(list_frame, activestyle="dotbox")
        self.chat_listbox.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_frame, command=self.chat_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.chat_listbox.config(yscrollcommand=scrollbar.set)

        export_frame = ttk.Frame(self)
        export_frame.pack(fill="x", **pad)
        ttk.Label(export_frame, text="Export as:").pack(side="left")
        self.format_var = tk.StringVar(value=FORMAT_TEXT)
        ttk.Combobox(
            export_frame, textvariable=self.format_var, values=FORMATS, state="readonly"
        ).pack(side="left", padx=(6, 0))

        self.export_button = ttk.Button(export_frame, text="Export…", command=self._on_export_clicked)
        self.export_button.pack(side="right")

        self.status_var = tk.StringVar(value="Loading chats…")
        ttk.Label(self, textvariable=self.status_var, foreground="#666").pack(fill="x", padx=10, pady=(0, 10))

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
        self.status_var.set("Loading chats…")
        self.export_button.state(["disabled"])

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
        self.status_var.set("Couldn't load chats.")
        messagebox.showerror("iMessage Export", message)

    def _on_load_success(self, db_path: str, chats: list[Chat], backup: Backup | None) -> None:
        self.db_path = db_path
        self.backup = backup
        self.chats = chats
        self.export_button.state(["!disabled"])
        self.status_var.set(f"{len(chats)} chats loaded.")
        self._render_chat_list()

    def _render_chat_list(self) -> None:
        self.chat_listbox.delete(0, "end")
        needle = self.filter_var.get().strip().lower()
        self._visible_chats = [
            c for c in self.chats if not needle or needle in c.label.lower()
        ]
        for chat in self._visible_chats:
            self.chat_listbox.insert("end", f"{chat.label}  ({chat.message_count} msgs)")

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
        self.status_var.set(f"Exporting {chat.label}…")

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
        self.export_button.state(["!disabled"])
        self.status_var.set("Export failed.")
        messagebox.showerror("iMessage Export", message)

    def _on_export_success(self, message: str) -> None:
        self.export_button.state(["!disabled"])
        self.status_var.set("Done.")
        messagebox.showinfo("iMessage Export", message)


def main() -> None:
    app = ExportApp()
    app.mainloop()


if __name__ == "__main__":
    main()
