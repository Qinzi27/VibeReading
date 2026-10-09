"""Append-only annotation versions, separated from the inspected source tree."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class ConflictError(ValueError):
    """An annotation or source version changed while the user was editing."""


class AnnotationStore:
    """Keep every annotation revision so later changes remain traceable."""

    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS annotations (
                project TEXT NOT NULL, symbol TEXT NOT NULL, version INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY (project, symbol, version))""")

    @contextmanager
    def connect(self):
        """Use one connection per operation, allowing HTTP worker threads."""
        database = sqlite3.connect(self.database, timeout=10)
        try:
            with database:
                yield database
        finally:
            # sqlite3's transaction context does not close Windows file handles.
            database.close()

    def latest(self, project: str) -> dict:
        with self.connect() as db:
            rows = db.execute("""SELECT a.symbol, a.payload FROM annotations a
                JOIN (SELECT symbol, MAX(version) AS version FROM annotations
                WHERE project=? GROUP BY symbol) b
                ON a.symbol=b.symbol AND a.version=b.version WHERE a.project=?""",
                (project, project)).fetchall()
        return {symbol: json.loads(payload) for symbol, payload in rows}

    def save(self, project: str, symbol: str, code_hash: str, expected_version: int,
             fields: dict) -> dict:
        """Append only after optimistic concurrency has been checked."""
        payload = {}
        for key in ("summary", "inputs", "outputs", "notes"):
            value = fields.get(key, "")
            if not isinstance(value, str) or len(value) > 12000:
                raise ValueError(f"字段 {key} 必须是最多 12000 字的文字")
            payload[key] = value
        origin = fields.get("origin", "manual")
        status = fields.get("status", "draft")
        if origin not in ("manual", "ai") or status not in ("draft", "approved"):
            raise ValueError("说明来源或审核状态无效")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT MAX(version) FROM annotations WHERE project=? AND symbol=?",
                                 (project, symbol)).fetchone()[0] or 0
            if current != expected_version:
                raise ConflictError("说明已有新版本，请保留当前草稿并重新读取后合并")
            payload.update(version=current + 1, origin=origin, status=status,
                           code_hash=code_hash, updated_at=datetime.now(timezone.utc).isoformat(),
                           approved_by="local_user" if status == "approved" else None)
            db.execute("INSERT INTO annotations VALUES (?,?,?,?)",
                       (project, symbol, current + 1, json.dumps(payload, ensure_ascii=False)))
        return payload
