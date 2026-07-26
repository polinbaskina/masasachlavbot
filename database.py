import sqlite3
import secrets
import string
from contextlib import contextmanager

DB_PATH = "graduation.db"


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graduates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                owner_code TEXT UNIQUE NOT NULL,
                guest_code TEXT UNIQUE NOT NULL,
                chat_id INTEGER UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                graduate_id INTEGER NOT NULL,
                sender_chat_id INTEGER,
                sender_name TEXT,
                is_anonymous INTEGER DEFAULT 0,
                text TEXT,
                photo_file_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (graduate_id) REFERENCES graduates(id)
            )
        """)
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _generate_code(length=8):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _unique_code(conn, column, length=8):
    while True:
        code = _generate_code(length)
        clash = conn.execute(
            f"SELECT 1 FROM graduates WHERE {column} = ?", (code,)
        ).fetchone()
        if not clash:
            return code


def bulk_create_graduates(names: list[str]):
    """Pre-registers graduates by name only (no chat_id yet). Returns list of dicts with codes."""
    created = []
    with get_conn() as conn:
        for name in names:
            name = name.strip()
            if not name:
                continue
            owner_code = "o_" + _unique_code(conn, "owner_code")
            guest_code = "g_" + _unique_code(conn, "guest_code")
            conn.execute(
                "INSERT INTO graduates (name, owner_code, guest_code) VALUES (?, ?, ?)",
                (name, owner_code, guest_code),
            )
            created.append({"name": name, "owner_code": owner_code, "guest_code": guest_code})
        conn.commit()
    return created


def bulk_create_with_ids(pairs: list[tuple[str, int]]):
    """Pre-registers graduates with their Telegram chat_id already known.
    They just need to press /start once and will be recognized immediately.
    Returns (created, errors) — errors lists (name, id, reason) for skipped rows."""
    created, errors = [], []
    with get_conn() as conn:
        for name, chat_id in pairs:
            name = name.strip()
            if not name:
                continue
            clash = conn.execute(
                "SELECT name FROM graduates WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if clash:
                errors.append((name, chat_id, f"этот ID уже привязан к «{clash['name']}»"))
                continue

            owner_code = "o_" + _unique_code(conn, "owner_code")
            guest_code = "g_" + _unique_code(conn, "guest_code")
            conn.execute(
                "INSERT INTO graduates (name, owner_code, guest_code, chat_id) VALUES (?, ?, ?, ?)",
                (name, owner_code, guest_code, chat_id),
            )
            created.append({"name": name, "chat_id": chat_id})
        conn.commit()
    return created, errors


def get_graduate_by_owner_code(code: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM graduates WHERE owner_code = ?", (code,)
        ).fetchone()


def get_graduate_by_guest_code(code: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM graduates WHERE guest_code = ?", (code,)
        ).fetchone()


def get_graduate_by_chat_id(chat_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM graduates WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def get_graduate_by_id(graduate_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM graduates WHERE id = ?", (graduate_id,)
        ).fetchone()


def activate_graduate(owner_code: str, chat_id: int):
    """Binds a Telegram chat_id to a pre-registered graduate.
    Returns (graduate_row, error) where error is None on success, or a string reason on failure."""
    with get_conn() as conn:
        graduate = conn.execute(
            "SELECT * FROM graduates WHERE owner_code = ?", (owner_code,)
        ).fetchone()
        if not graduate:
            return None, "not_found"
        if graduate["chat_id"] is not None and graduate["chat_id"] != chat_id:
            return graduate, "already_claimed"
        if graduate["chat_id"] == chat_id:
            return graduate, None  # already activated by this same user, idempotent

        already_other = conn.execute(
            "SELECT 1 FROM graduates WHERE chat_id = ? AND id != ?", (chat_id, graduate["id"])
        ).fetchone()
        if already_other:
            return graduate, "user_has_other_profile"

        conn.execute(
            "UPDATE graduates SET chat_id = ? WHERE id = ?", (chat_id, graduate["id"])
        )
        conn.commit()
        graduate = conn.execute(
            "SELECT * FROM graduates WHERE id = ?", (graduate["id"],)
        ).fetchone()
        return graduate, None


def _normalize_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def claim_by_name(name: str, chat_id: int):
    """Binds a chat_id to a pre-registered graduate by matching their name
    (e.g. from a /claim command sent in a group chat). Case/space-insensitive exact match.
    Returns (graduate_row, error) where error is None on success."""
    target = _normalize_name(name)
    with get_conn() as conn:
        already_other = conn.execute(
            "SELECT 1 FROM graduates WHERE chat_id = ?", (chat_id,)
        ).fetchone()

        rows = conn.execute("SELECT * FROM graduates").fetchall()
        match = next((r for r in rows if _normalize_name(r["name"]) == target), None)

        if not match:
            return None, "not_found"
        if match["chat_id"] is not None and match["chat_id"] != chat_id:
            return match, "already_claimed"
        if match["chat_id"] == chat_id:
            return match, None  # idempotent
        if already_other:
            return match, "user_has_other_profile"

        conn.execute("UPDATE graduates SET chat_id = ? WHERE id = ?", (chat_id, match["id"]))
        conn.commit()
        match = conn.execute("SELECT * FROM graduates WHERE id = ?", (match["id"],)).fetchone()
        return match, None


def save_message(
    graduate_id: int,
    sender_chat_id: int,
    sender_name: str,
    is_anonymous: bool,
    text: str = None,
    photo_file_id: str = None,
):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO messages
               (graduate_id, sender_chat_id, sender_name, is_anonymous, text, photo_file_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (graduate_id, sender_chat_id, sender_name, int(is_anonymous), text, photo_file_id),
        )
        conn.commit()
        return cur.lastrowid


def get_messages_for_graduate(graduate_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM messages WHERE graduate_id = ? ORDER BY created_at ASC",
            (graduate_id,),
        ).fetchall()


def all_graduates():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM graduates ORDER BY name ASC").fetchall()


def count_graduates():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM graduates").fetchone()["c"]


def count_activated():
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM graduates WHERE chat_id IS NOT NULL"
        ).fetchone()["c"]


def count_messages():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
