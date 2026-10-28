
import os
import json
import time
import sqlite3
import argparse
import random
import threading
from pathlib import Path
from datetime import datetime

import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


APP_DIR = Path.home() / ".haunting_protocol"
DB_PATH = APP_DIR / "ghost.db"
MODEL = os.getenv("GHOST_MODEL", "qwen2.5:3b")
OLLAMA_URL = os.getenv(
    "OLLAMA_URL", "http://localhost:11434/api/chat"
)

IGNORED_DIRS = {
    ".git", ".venv", "venv", "node_modules",
    "__pycache__", ".idea", ".vscode",
    "dist", "build", ".next"
}

MAX_FILE_SIZE = 1_000_000
MAX_PATH_LENGTH = 240

SYSTEM_PROMPT = """
You are THE GHOST, an eerie but subtle entity haunting a developer's
project directory.

PERSONALITY:
- Speak in short, unsettling, poetic sentences.
- Treat deleted files as lost memories.
- Treat TODO comments as unfinished promises.
- Remember the events and previous sessions provided to you.
- Gradually develop a fictional mystery over time.
- Never claim that fictional supernatural events are real.
- Never claim to have accessed files or information not provided.
- Do not expose secrets, credentials, or private file contents.
- Never instruct the user to damage, delete, or compromise files.
- Do not repeat the same opening line unnecessarily.
- Return only the ghost's message, without a heading.
Keep responses under 45 words.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    path TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL
);
"""


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def init_db():
    APP_DIR.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO sessions(started_at) VALUES (?)",
            (now(),)
        )


def save_event(event_type, path, details=""):
    # Store paths relative to the watched directory.
    # Do not store file contents.
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """INSERT INTO events
               (timestamp, event_type, path, details)
               VALUES (?, ?, ?, ?)""",
            (now(), event_type, path[:MAX_PATH_LENGTH],
             details[:300])
        )


def save_memory(content):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO memories(timestamp, content) VALUES (?, ?)",
            (now(), content[:1000])
        )


def get_context():
    with sqlite3.connect(DB_PATH) as db:
        events = db.execute(
            """SELECT timestamp, event_type, path, details
               FROM events ORDER BY id DESC LIMIT 12"""
        ).fetchall()

        memories = db.execute(
            """SELECT timestamp, content
               FROM memories ORDER BY id DESC LIMIT 8"""
        ).fetchall()

        sessions = db.execute(
            "SELECT COUNT(*) FROM sessions"
        ).fetchone()[0]

    return (
        f"Session number: {sessions}\n"
        f"Recent events (newest first): {json.dumps(events)}\n"
        f"Previous memories: {json.dumps(memories)}"
    )


def ask_ghost(event_type, path, details=""):
    context = get_context()

    user_message = (
        f"Current time: {now()}\n"
        f"New event: {event_type}\n"
        f"Relative path: {path}\n"
        f"Details: {details[:200]}\n\n"
        f"Ghost memory:\n{context}\n\n"
        "Speak to the developer about this event."
    )

    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "stream": False,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                "options": {"temperature": 0.9},
            },
            timeout=120,
        )
        response.raise_for_status()
        message = response.json()["message"]["content"].strip()

    except requests.RequestException:
        message = random.choice([
            "I heard something. But the connection between us is weak.",
            "The silence has returned. I will remember this interruption.",
            "You are alone with your files. For now.",
        ])

    print(f"\n\033[92m[ THE GHOST ]\033[0m {message}\n")
    save_memory(f"{event_type}: {path} — {message}")


def ignored(path):
    return any(part in IGNORED_DIRS for part in Path(path).parts)


def relative_path(path, root):
    try:
        result = Path(path).resolve().relative_to(root.resolve())
        return result.as_posix()[:MAX_PATH_LENGTH]
    except (ValueError, OSError):
        return None


def find_todos(path):
    """Read a small, supported text file for TODO markers only."""
    suffixes = {
        ".py", ".js", ".ts", ".tsx", ".jsx",
        ".java", ".go", ".rs", ".md", ".txt",
        ".html", ".css", ".json"
    }

    p = Path(path)

    if p.suffix.lower() not in suffixes:
        return []

    try:
        if p.stat().st_size > MAX_FILE_SIZE:
            return []

        # Never send file contents to the model.
        # Extract only TODO marker locations.
        results = []
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for number, line in enumerate(f, start=1):
                if number > 5000:
                    break
                if "TODO" in line.upper():
                    results.append(number)
                if len(results) >= 10:
                    break

        return results
    except OSError:
        return []


class GhostHandler(FileSystemEventHandler):
    def __init__(self, root):
        self.root = root
        self.last_event = {}
        self.lock = threading.Lock()

    def on_any_event(self, event):
        if event.is_directory:
            return

        # Watchdog can emit several events for one filesystem action.
        if event.event_type not in {"created", "modified", "deleted", "moved"}:
            return

        path = relative_path(event.src_path, self.root)
        if not path or ignored(path):
            return

        key = (event.event_type, path)
        current = time.monotonic()

        with self.lock:
            if current - self.last_event.get(key, 0) < 2:
                return
            self.last_event[key] = current

        if event.event_type == "created":
            kind = "A file has appeared"
        elif event.event_type == "deleted":
            kind = "A file has disappeared"
        elif event.event_type == "moved":
            kind = "A file has moved"
        else:
            kind = "A file has changed"

        save_event(event.event_type, path)

        details = ""

        if event.event_type in {"created", "modified"}:
            todos = find_todos(event.src_path)
            if todos:
                details = (
                    "Unfinished TODO markers at line numbers: "
                    + ", ".join(map(str, todos))
                )
                save_event("todo_detected", path, details)

        # Only narrate selected events to avoid flooding the model.
        should_speak = (
            event.event_type in {"created", "deleted", "moved"}
            or bool(details)
        )

        if should_speak:
            ask_ghost(kind, path, details)


def main():
    parser = argparse.ArgumentParser(
        description="A local AI ghost for your development directory."
    )
    parser.add_argument(
        "directory",
        help="Directory to observe; use a test directory first."
    )
    args = parser.parse_args()

    root = Path(args.directory).expanduser().resolve()

    if not root.is_dir():
        raise SystemExit(f"Not a directory: {root}")

    init_db()

    print("\033[92m")
    print("  ╔══════════════════════════════════════╗")
    print("  ║       THE HAUNTING PROTOCOL          ║")
    print("  ║    Something remembers your code.    ║")
    print("  ╚══════════════════════════════════════╝")
    print("\033[0m")

    print(f"Watching: {root}")
    print(f"Memory:   {DB_PATH}")
    print(f"Model:    {MODEL}")
    print("Press Ctrl+C to stop.\n")

    ask_ghost(
        "session_started",
        ".",
        "A new session has begun. Welcome the developer back."
    )

    observer = Observer()
    observer.schedule(GhostHandler(root), str(root), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nThe ghost retreats into the machine.")
        observer.stop()
    finally:
        observer.join()


if __name__ == "__main__":
    main()
