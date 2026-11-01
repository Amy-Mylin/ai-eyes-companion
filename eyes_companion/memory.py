"""保存用户明确要求保留的条目；纠正和删除立即提交。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4


class MemoryStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
            INSERT OR IGNORE INTO meta VALUES (1, 0);
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, content TEXT NOT NULL, context TEXT NOT NULL,
                source TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        self.db.commit()

    def revision(self):
        with self.lock:
            return self.db.execute("SELECT revision FROM meta WHERE id=1").fetchone()[0]

    def list(self):
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM memories ORDER BY updated_at DESC, id")]

    def put(self, content, *, memory_id=None, context="用户明确要求记住", source="user"):
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 800:
            raise ValueError("记忆内容必须为1至800个字符")
        if not isinstance(context, str) or len(context) > 200:
            raise ValueError("适用情境过长")
        with self.lock, self.db:
            if memory_id is not None and not self.db.execute(
                    "SELECT 1 FROM memories WHERE id=?", (memory_id,)).fetchone():
                raise KeyError("要修改的记忆已经不存在")
            memory_id = memory_id or uuid4().hex
            self.db.execute("INSERT OR REPLACE INTO memories VALUES (?, ?, ?, ?, ?)",
                            (memory_id, content.strip(), context, source,
                             datetime.now(timezone.utc).isoformat()))
            self.db.execute("UPDATE meta SET revision=revision+1 WHERE id=1")
            return memory_id

    def delete(self, memory_id):
        with self.lock, self.db:
            changed = self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,)).rowcount
            if changed:
                self.db.execute("UPDATE meta SET revision=revision+1 WHERE id=1")
            return bool(changed)

    def relevant(self, query, limit=4):
        # 首版只有少量明确条目；中文用双字片段排序，不假装具备语义向量检索。
        terms = {query[i:i + 2] for i in range(max(0, len(query) - 1))}
        rows = self.list()
        ranked = sorted(rows, key=lambda row: sum(t in row["content"] for t in terms), reverse=True)
        return ranked[:limit]

    def export(self):
        return json.dumps({"revision": self.revision(), "memories": self.list()},
                          ensure_ascii=False, indent=2)

    def close(self):
        with self.lock:
            self.db.close()
