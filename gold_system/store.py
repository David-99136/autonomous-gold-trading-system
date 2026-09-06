"""SQLite WAL 稽核與持久化狀態。每次意圖送出前先落盤。"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, time TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        self.db.commit()

    def emit(self, kind, **payload):
        # 只接受明確的業務欄位；API 原始回應不直接寫進這裡。
        sensitive = ("password", "token", "secret", "api_key", "identifier", "cst")

        def clean(value):
            if isinstance(value, dict):
                return {k: "[REDACTED]" if any(s in k.lower() for s in sensitive) else clean(v)
                        for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value

        self.db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)",
                        (datetime.now(timezone.utc).isoformat(), kind,
                         json.dumps(clean(payload), default=str, ensure_ascii=False)))
        self.db.commit()

    def claim(self, signal_id):
        try:
            self.db.execute("INSERT INTO intents VALUES(?, 'PENDING')", (signal_id,))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def finish(self, signal_id, status):
        self.db.execute("UPDATE intents SET state=? WHERE id=?", (status, signal_id))
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value, default=str)))
        self.db.commit()

    def events(self):
        return [{"id": r[0], "time": r[1], "kind": r[2], "payload": json.loads(r[3])}
                for r in self.db.execute("SELECT * FROM events ORDER BY id")]

    def close(self):
        self.db.close()
