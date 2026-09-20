import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .domain import iso, uid, utcnow

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS personas(id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT,data TEXT);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,session TEXT,role TEXT,content TEXT,at TEXT,revision INTEGER,
  visible INTEGER DEFAULT 1, source_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,content TEXT,subject TEXT,kind TEXT,source_type TEXT,
 source_ids TEXT,occurred_at TEXT,created TEXT,entities TEXT,topics TEXT,importance REAL,confidence REAL,
 status TEXT DEFAULT 'active',pinned INTEGER DEFAULT 0,unresolved INTEGER DEFAULT 0,last_used TEXT,
 supersedes TEXT,vector TEXT,summary_of TEXT);
CREATE TABLE IF NOT EXISTS links(source TEXT,target TEXT,kind TEXT,strength REAL,PRIMARY KEY(source,target,kind));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,due TEXT,status TEXT,revision INTEGER,payload TEXT,created TEXT);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,due);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,session TEXT,content TEXT,status TEXT,revision INTEGER,
 proactive INTEGER,created TEXT,detail TEXT,used_ids TEXT);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,at TEXT,until_at TEXT,title TEXT,outline TEXT,content TEXT,
 status TEXT,persona_version INTEGER,day TEXT);
CREATE TABLE IF NOT EXISTS diaries(id TEXT PRIMARY KEY,day TEXT,created TEXT,content TEXT,source_ids TEXT,visible INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,kind TEXT,detail TEXT);
CREATE TABLE IF NOT EXISTS retrievals(id TEXT PRIMARY KEY,at TEXT,query TEXT,results TEXT);
"""


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as c:
            c.executescript(SCHEMA)
        self.set_default("revision", 0)
        self.set_default("paused", False)
        self.set_default("state", "尚未开始今天的生活")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connect() as c:
            return c.execute(sql, args).rowcount

    def get(self, key, default=None):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, dump(value)))

    def set_default(self, key, value):
        self.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, dump(value)))

    def audit(self, kind, detail):
        self.execute("INSERT INTO audit(at,kind,detail) VALUES (?,?,?)", (iso(utcnow()), kind, dump(detail)))

    def persona(self):
        row = self.one("SELECT * FROM personas ORDER BY id DESC LIMIT 1")
        return {"version": row["id"], **json.loads(row["data"])} if row else None

    def save_persona(self, persona, now=None):
        now = now or utcnow()
        existing = self.persona()
        with self.connect() as c:
            c.execute("INSERT INTO personas(created,data) VALUES (?,?)", (iso(now), dump(persona)))
            c.execute("UPDATE events SET status='cancelled' WHERE status='planned'")
            if existing:
                rev = self.get("revision") + 1
                c.execute("UPDATE meta SET value=? WHERE key='revision'", (dump(rev),))
                c.execute(
                    "UPDATE jobs SET status='cancelled' WHERE status='pending' AND kind IN ('turn','proactive','reply')"
                )
                c.execute("UPDATE outbox SET status='cancelled' WHERE status='pending'")
        self.audit("persona.updated", {"version": self.persona()["version"]})

    def add_job(self, kind, due, payload, revision=None):
        job_id = uid()
        self.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            (
                job_id,
                kind,
                iso(due),
                "pending",
                self.get("revision") if revision is None else revision,
                dump(payload),
                iso(utcnow()),
            ),
        )
        return job_id

    def recover(self):
        # A crash after handing bytes to the transport cannot safely be retried.
        self.execute(
            "UPDATE outbox SET status='unknown',detail='进程中断，发送结果未知' WHERE status='sending'"
        )
        self.execute("UPDATE jobs SET status='pending' WHERE status='running'")
