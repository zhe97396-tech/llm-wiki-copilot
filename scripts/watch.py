#!/usr/bin/env python3
"""
INBOX Watcher for LLM Wiki.

Monitors 00-INBOX/ for new files and triggers compilation.
When a new file appears, it prints a notification that Hermes can pick up
and act on (by running the PROMPT.md compilation flow).

Usage:
    python scripts/watch.py                    # watch continuously (foreground)
    python scripts/watch.py --once             # check once, print pending files, exit
    python scripts/watch.py --once --json      # same, but JSON output

Design:
    - Uses watchdog for filesystem events
    - Debounces rapid changes (e.g. file copy produces multiple events)
    - Ignores temp files (starting with ~ or .)
    - Cross-references with processed.md to only report truly new files
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Bootstrap shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from _utils import (
    DATA_ROOT, INBOX_DIR, PROCESSED_FILE,
    read_file, sha256, load_processed, is_processed,
)

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False
    # 占位类防止模块级 NameError（watch_continuous() 会先检查 HAS_WATCHDOG 并退出）
    FileSystemEventHandler = object

# ── Constants ──────────────────────────────────────────────────────────

DEBOUNCE_SECONDS = 2  # Wait this long after last change before processing
IGNORE_PREFIXES = ("~", ".", "$")  # Temp file prefixes
IGNORE_EXTENSIONS = (".tmp", ".crdownload", ".part")  # In-progress download extensions
VALID_EXTENSIONS = (".md", ".txt", ".pdf", ".docx", ".pptx", ".png", ".jpg", ".jpeg", ".gif", ".webp")


# ── Pending file detection ─────────────────────────────────────────────

def find_pending_files() -> list[dict]:
    """Scan 00-INBOX/ for files not yet in processed.md.

    Returns list of {path, name, size, hash, is_folder}.
    """
    if not INBOX_DIR.exists():
        return []

    processed = load_processed()
    pending = []

    for item in sorted(INBOX_DIR.iterdir()):
        name = item.name
        # Skip ignored files
        if any(name.startswith(p) for p in IGNORE_PREFIXES):
            continue
        if any(name.endswith(e) for e in IGNORE_EXTENSIONS):
            continue

        if item.is_dir():
            # Folder = package (e.g. multi-image article)
            # Check if any file inside has been processed
            folder_files = list(item.rglob("*"))
            folder_names = [f.name for f in folder_files if f.is_file()]
            # If none of the folder's files are in processed, it's pending
            if not any(fn in processed for fn in folder_names):
                pending.append({
                    "path": str(item.relative_to(DATA_ROOT)),
                    "name": name,
                    "size": sum(f.stat().st_size for f in folder_files if f.is_file()),
                    "hash": "",
                    "is_folder": True,
                    "file_count": len(folder_files),
                })
        elif item.is_file():
            if name in processed:
                continue
            # Compute hash for change detection
            try:
                content = item.read_text(encoding="utf-8", errors="replace")
                content_hash = sha256(content)
            except Exception:
                content_hash = "unreadable"
            pending.append({
                "path": str(item.relative_to(DATA_ROOT)),
                "name": name,
                "size": item.stat().st_size,
                "hash": content_hash,
                "is_folder": False,
            })

    return pending


# ── Watcher (continuous mode) ──────────────────────────────────────────

class InboxHandler(FileSystemEventHandler):
    """Watchdog handler that detects new files in 00-INBOX/."""

    def __init__(self, debounce: float = DEBOUNCE_SECONDS):
        self._last_event_time: dict[str, float] = {}
        self.debounce = debounce
        self.pending: set[str] = set()

    def _register(self, path: str) -> None:
        name = Path(path).name
        if any(name.startswith(p) for p in IGNORE_PREFIXES):
            return
        if any(name.endswith(e) for e in IGNORE_EXTENSIONS):
            return
        self._last_event_time[path] = time.time()
        self.pending.add(path)

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._register(event.src_path)

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._register(event.src_path)

    def get_ready(self) -> list[str]:
        """Return paths that haven't changed for debounce seconds."""
        now = time.time()
        ready = []
        still_pending = set()
        for path in self.pending:
            if now - self._last_event_time.get(path, 0) >= self.debounce:
                ready.append(path)
            else:
                still_pending.add(path)
        self.pending = still_pending
        return ready


def watch_continuous() -> None:
    """Watch 00-INBOX/ continuously, print notifications on new files."""
    if not HAS_WATCHDOG:
        print("ERROR: watchdog not installed. Run: pip install watchdog")
        sys.exit(1)

    handler = InboxHandler()
    observer = Observer()
    observer.schedule(handler, str(INBOX_DIR), recursive=True)
    observer.start()

    print(f"👀 Watching {INBOX_DIR} for new files... (Ctrl+C to stop)")

    try:
        while True:
            time.sleep(1)
            ready = handler.get_ready()
            if ready:
                for path in ready:
                    name = Path(path).name
                    print(f"📥 New file detected: {name}")
                    print(f"   Path: {path}")
                    print(f"   Action: Run compilation flow (PROMPT.md)")
    except KeyboardInterrupt:
        observer.stop()
        print("\n👋 Watcher stopped.")
    observer.join()


# ── Main ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="INBOX Watcher for LLM Wiki")
    parser.add_argument("--once", action="store_true",
                        help="Check once and exit (don't watch continuously)")
    parser.add_argument("--json", action="store_true",
                        help="Output in JSON format (only with --once)")
    args = parser.parse_args()

    if args.once:
        pending = find_pending_files()
        if args.json:
            print(json.dumps(pending, ensure_ascii=False, indent=2))
        else:
            if not pending:
                print("✅ 00-INBOX is empty, no pending files.")
            else:
                print(f"📥 Found {len(pending)} pending file(s) in 00-INBOX:")
                for f in pending:
                    suffix = f" (folder, {f['file_count']} files)" if f["is_folder"] else ""
                    print(f"  - {f['name']}{suffix}")
    else:
        watch_continuous()


if __name__ == "__main__":
    main()
