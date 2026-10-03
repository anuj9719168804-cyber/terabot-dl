"""Local SQLite replacement for Firestore (no Firebase needed)."""
import os, sqlite3, logging
from dotenv import load_dotenv
load_dotenv()
log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "local.db")

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init():
    conn = _get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id TEXT PRIMARY KEY,
        username TEXT,
        last_active REAL,
        mode TEXT DEFAULT 'exp'
    );
    CREATE TABLE IF NOT EXISTS cache (
        bucket TEXT,
        key TEXT,
        message_id INTEGER,
        PRIMARY KEY (bucket, key)
    );
    """)
    conn.commit()
    conn.close()

_init()

class _DocRef:
    def __init__(self, table, doc_id):
        self.table = table
        self.id = doc_id
    def _conn(self):
        return _get_conn()
    def set(self, data, merge=False):
        conn = self._conn()
        if self.table == "users":
            row = conn.execute("SELECT * FROM users WHERE chat_id=?", (self.id,)).fetchone()
            base = dict(row) if (merge and row) else {}
            base.update({k: v for k, v in data.items()})
            conn.execute(
                "INSERT INTO users (chat_id, username, last_active, mode) VALUES (?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, last_active=excluded.last_active, mode=excluded.mode",
                (self.id, base.get("username"), base.get("last_active"), base.get("mode", "exp")))
        else:  # cache bucket: data = {safe_key: msg_id}
            for k, v in data.items():
                conn.execute(
                    "INSERT INTO cache (bucket, key, message_id) VALUES (?,?,?) "
                    "ON CONFLICT(bucket, key) DO UPDATE SET message_id=excluded.message_id",
                    (self.id, k, v))
        conn.commit(); conn.close()
    def update(self, data):
        conn = self._conn()
        sets = ", ".join(f"{k}=?" for k in data)
        conn.execute(f"UPDATE users SET {sets} WHERE chat_id=?", (*data.values(), self.id))
        conn.commit(); conn.close()
    def get(self, field_paths=None):
        conn = self._conn()
        row = conn.execute("SELECT * FROM users WHERE chat_id=?", (self.id,)).fetchone()
        conn.close()
        return _Snap(dict(row) if row else None)
    def stream(self):
        conn = self._conn()
        rows = conn.execute("SELECT * FROM users").fetchall()
        conn.close()
        return [_Snap(dict(r), r["chat_id"]) for r in rows]

class _Snap:
    def __init__(self, data, doc_id=None):
        self._data = data or {}
        self.id = doc_id
    @property
    def exists(self):
        return bool(self._data)
    def to_dict(self):
        return self._data
    def get(self, field_paths=None):  # cache bucket get
        return self

class _Collection:
    def __init__(self, table):
        self.table = table
    def document(self, doc_id):
        return _DocRef(self.table, doc_id)
    def stream(self):
        conn = _get_conn()
        rows = conn.execute("SELECT * FROM users").fetchall()
        conn.close()
        return [_Snap(dict(r), r["chat_id"]) for r in rows]

class _DB:
    def collection(self, name):
        return _Collection(name)

db = _DB()
