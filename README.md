# imessage-export

Command-line tool for reading and exporting conversations from the macOS
Messages database (`~/Library/Messages/chat.db`), including text, photos and
other attachments, and a combined PDF — either from the live database on
this Mac, or from a local (unencrypted) iPhone backup made with Finder.

This is a small, focused Python tool — not a full port of
[imessage-exporter](https://github.com/ReagentX/imessage-exporter) (the
Rust project this was originally scoped against). It covers plain-text
messages and their attachments; exotic message types (polls, Digital Touch,
handwriting, business chat, etc.) aren't specially handled.

## Requirements

- macOS (the Messages database format and `sips` HEIC conversion are
  macOS-specific)
- Python 3.9+
- **Full Disk Access** for whichever terminal/app runs this tool. macOS
  gates read access to `~/Library/Messages/chat.db` behind this even for
  your own account. Grant it under **System Settings → Privacy & Security →
  Full Disk Access**, then fully quit and reopen your terminal.

## Install

```bash
python3 -m pip install --user -e .
```

This installs the `imessage-export` command (via `pyproject.toml`'s
`[project.scripts]` entry point). If the command isn't found afterward, make
sure `~/Library/Python/<version>/bin` is on your `PATH`.

Without installing, every command also works as `python3 messages_cli.py ...`
from the repo root.

## Usage

Every command that targets a specific chat accepts either `--chat-id N`
(from `list-chats`) or `--identifier <phone-or-email>`, and either reads the
live Mac database (default) or a local iPhone backup via `--backup`.

```bash
# List every chat with its ROWID, message count, and label.
imessage-export list-chats

# Export one chat's text messages to a file (or stdout).
imessage-export export --chat-id 42 -o conversation.txt

# Copy every attachment (photos, videos, etc.) out of a chat.
imessage-export export-attachments --identifier +15551234567 -o ./attachments

# Render a chat (text + inline photos) to a single PDF.
imessage-export export-pdf --chat-id 42 -o conversation.pdf

# Check how far back a chat's local history actually goes.
imessage-export info --chat-id 42

# List local iPhone backups Finder has made on this Mac.
imessage-export list-backups

# Read from an iPhone backup instead of the live Mac DB.
imessage-export list-chats --backup <udid-or-prefix>
```

Run `imessage-export <command> --help` for the full flag list.

### Reading from an iPhone backup

The Mac's local `chat.db` isn't always the full history — "Messages in
iCloud" syncing is lazy, and history can be capped by the "Keep Messages"
retention setting. If an iPhone has a longer local history, you can read
straight from a **local, unencrypted** Finder backup instead:

1. Connect the iPhone, open Finder, select it in the sidebar.
2. Under **General**, choose "Back up all data on your iPhone to this Mac",
   make sure **"Encrypt local backup" is unchecked**, then **Back Up Now**.
3. `imessage-export list-backups` to find its UDID.
4. Pass `--backup <udid-or-prefix>` to any command.

Encrypted backups aren't supported (decrypting them requires implementing
Apple's backup keybag/AES scheme, which this tool doesn't do).

## Known limitations

- **`attributedBody` decoding is heuristic**, not a full typedstream parser:
  it looks for the first `NSString` value in the blob, which is the plain
  text for ordinary messages but can misfire on messages with unusual rich
  formatting.
- **PDF emoji**: text renders with a bundled macOS Unicode font
  (`Arial Unicode.ttf`) that doesn't include color emoji glyphs, so emoji
  show as blank space rather than crashing or garbling the page.
- **HEIC photos** are converted to JPEG via macOS's `sips` for PDF
  embedding; if `sips` is unavailable the original file is embedded as-is
  (and will likely fail to render as an image).
- **Video/audio attachments** aren't embedded in the PDF, just noted with a
  placeholder line.

## Development

```bash
python3 -m pip install --user -e ".[dev]"
python3 -m pytest
```

Tests use synthetic fixtures (an in-memory SQLite database shaped like
`chat.db`, fabricated backup directories, generated images) — no real
Messages data is read or required to run the suite.

## Project layout

- `messages_library.py` — core DB access: WAL-safe snapshotting, Apple
  timestamp conversion, `attributedBody` decoding, chat/message/attachment
  queries.
- `ios_backup.py` — locates and reads `sms.db` and attachments out of a
  local Finder iPhone backup.
- `pdf_export.py` — renders a chat to a single PDF with inline photos.
- `messages_cli.py` — the CLI tying it all together.
- `tests/` — the test suite.
