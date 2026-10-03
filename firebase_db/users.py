"""Local SQLite user tracking."""
import logging, time
from .db import db
log = logging.getLogger(__name__)
MODE = str
_USERS_CACHE = {}
_DEBOUNCE = 900

def track_user(chat_id, username):
    uid = str(chat_id); now = time.time()
    cached = _USERS_CACHE.get(uid, {})
    if cached and (now - cached.get("last_active", 0)) < _DEBOUNCE:
        return
    try:
        ref = db.collection("users").document(uid)
        snap = ref.get()
        if snap.exists:
            ref.update({"last_active": now})
            _USERS_CACHE[uid] = {**snap.to_dict(), "last_active": now}
        else:
            data = {"username": username, "last_active": now, "mode": "exp"}
            ref.set(data)
            _USERS_CACHE[uid] = data
    except Exception as e:
        log.error(f"track_user failed: {e}")

def get_user_mode(chat_id):
    uid = str(chat_id)
    if uid in _USERS_CACHE:
        return _USERS_CACHE[uid].get("mode", "exp")
    try:
        snap = db.collection("users").document(uid).get()
        if snap.exists:
            _USERS_CACHE[uid] = snap.to_dict()
            return _USERS_CACHE[uid].get("mode", "exp")
    except Exception as e:
        log.error(f"get_user_mode failed: {e}")
    return "exp"

def set_user_mode(chat_id, mode):
    uid = str(chat_id)
    try:
        db.collection("users").document(uid).set({"mode": mode}, merge=True)
        if uid in _USERS_CACHE:
            _USERS_CACHE[uid]["mode"] = mode
        else:
            _USERS_CACHE[uid] = {"mode": mode}
        return True
    except Exception as e:
        log.error(f"set_user_mode failed: {e}")
        return False

def get_all_users():
    try:
        return {d.id: d.to_dict() for d in db.collection("users").stream()}
    except Exception as e:
        log.error(f"get_all_users failed: {e}")
        return {}
