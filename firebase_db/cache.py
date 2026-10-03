"""Local SQLite video cache."""
import base64, logging, time
from .db import db
log = logging.getLogger(__name__)
MODE = str
_BUCKETS = ("get", "exp", "exphd", "dw")
_RANDOM_SNAPSHOT = {"data": {}, "timestamp": 0.0}
_RANDOM_TTL_SECONDS = 900

def _encode_key(surl):
    return "k_" + base64.urlsafe_b64encode(surl.encode()).decode().rstrip("=").replace("-", "_")

def _decode_key(field):
    b64 = field[2:].replace("_", "-")
    return base64.urlsafe_b64decode(b64 + "=" * ((-len(b64)) % 4)).decode()

def add_to_cache(surl, message_id, user_mode) -> bool:
    try:
        conn = db.collection("cache").document(user_mode)
        from .db import _get_conn
        c = _get_conn()
        c.execute("INSERT INTO cache (bucket, key, message_id) VALUES (?,?,?) ON CONFLICT(bucket,key) DO UPDATE SET message_id=excluded.message_id",
                  (user_mode, _encode_key(surl), message_id))
        c.commit(); c.close()
        _RANDOM_SNAPSHOT["timestamp"] = 0.0
        return True
    except Exception as e:
        log.error(f"add_to_cache failed: {e}")
        return False

def search_in_cache(surl, user_mode) -> int:
    from .db import _get_conn
    if user_mode == "get":
        order = ["exphd", "exp", "get"]
    elif user_mode == "exp":
        order = ["exphd", "exp"]
    elif user_mode == "dw":
        order = ["dw"]
    else:
        order = ["exphd"]
    key = _encode_key(surl)
    try:
        c = _get_conn()
        for bucket in order:
            row = c.execute("SELECT message_id FROM cache WHERE bucket=? AND key=?", (bucket, key)).fetchone()
            if row:
                c.close()
                return int(row["message_id"])
        c.close()
    except Exception as e:
        log.error(f"search_in_cache failed: {e}")
    return -1

def get_cache_for_random() -> dict:
    from .db import _get_conn
    if time.time() - _RANDOM_SNAPSHOT["timestamp"] < _RANDOM_TTL_SECONDS and _RANDOM_SNAPSHOT["data"]:
        return _RANDOM_SNAPSHOT["data"]
    merged = {}
    try:
        c = _get_conn()
        for bucket in _BUCKETS:
            for row in c.execute("SELECT key, message_id FROM cache WHERE bucket=?", (bucket,)):
                merged[_decode_key(row["key"])] = row["message_id"]
        c.close()
    except Exception as e:
        log.error(f"get_cache_for_random failed: {e}")
    _RANDOM_SNAPSHOT["data"] = merged
    _RANDOM_SNAPSHOT["timestamp"] = time.time()
    return merged
