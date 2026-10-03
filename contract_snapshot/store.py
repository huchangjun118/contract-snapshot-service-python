"""持久化存储：快照版本、基线指针、判定记录。

设计要点：
- SQLite 单文件存储，WAL 模式支持并发读写；
- 所有多步写入包在一个事务里，要么完整提交要么整体回滚，
  不会留下写了一半的中间产物；
- 基线（baseline）是单行指针，更新与读取都是原子的，
  任一时刻读到的都是某个已提交的完整版本；
- 读取基线按版本表主键查询，不随历史版本数量线性退化。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass

from .errors import ContractError, FailureKind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version_id   TEXT PRIMARY KEY,
    endpoint     TEXT NOT NULL,
    schema_json  TEXT NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    approved     INTEGER NOT NULL DEFAULT 0,
    created_seq  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versions_endpoint ON versions(endpoint, created_seq);

CREATE TABLE IF NOT EXISTS baselines (
    endpoint   TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES versions(version_id)
);

CREATE TABLE IF NOT EXISTS seq (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    val INTEGER NOT NULL
);
INSERT OR IGNORE INTO seq (id, val) VALUES (1, 0);
"""


@dataclass(frozen=True)
class SnapshotRecord:
    """一条已固化的快照版本。"""

    version_id: str
    endpoint: str
    schema: dict
    rules_fingerprint: str
    approved: bool
    created_seq: int


class SnapshotStore:
    """快照与基线的持久化存储。线程安全。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        try:
            # check_same_thread=False + 显式锁：多线程共享一个连接，
            # 内存库在测试与并发场景下也能看到同一份数据。
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            with self._conn:
                self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise ContractError(
                FailureKind.STORE_UNAVAILABLE, f"存储初始化失败: {exc}"
            ) from exc

    # ---- 内部工具 -------------------------------------------------

    def _run(self, fn):
        """在锁内执行，sqlite 错误统一映射为 STORE_UNAVAILABLE。"""
        with self._lock:
            try:
                return fn()
            except ContractError:
                raise
            except sqlite3.Error as exc:
                raise ContractError(
                    FailureKind.STORE_UNAVAILABLE, f"存储操作失败: {exc}"
                ) from exc

    @staticmethod
    def _row_to_record(row: sqlite3.Row | tuple) -> SnapshotRecord:
        return SnapshotRecord(
            version_id=row[0],
            endpoint=row[1],
            schema=json.loads(row[2]),
            rules_fingerprint=row[3],
            approved=bool(row[4]),
            created_seq=row[5],
        )

    # ---- 版本写入 -------------------------------------------------

    def save_version(
        self,
        version_id: str,
        endpoint: str,
        schema: dict,
        rules_fingerprint: str,
    ) -> SnapshotRecord:
        """保存一个未批准的快照版本（幂等：同 version_id 重复写入返回已有记录）。"""

        def op() -> SnapshotRecord:
            with self._conn:  # 事务：序号分配与版本插入同生共死
                existing = self._conn.execute(
                    "SELECT * FROM versions WHERE version_id = ?", (version_id,)
                ).fetchone()
                if existing:
                    return self._row_to_record(existing)
                seq = self._conn.execute(
                    "UPDATE seq SET val = val + 1 WHERE id = 1 RETURNING val"
                ).fetchone()[0]
                self._conn.execute(
                    "INSERT INTO versions"
                    " (version_id, endpoint, schema_json, rules_fingerprint, approved, created_seq)"
                    " VALUES (?, ?, ?, ?, 0, ?)",
                    (version_id, endpoint, json.dumps(schema, sort_keys=True),
                     rules_fingerprint, seq),
                )
                return SnapshotRecord(version_id, endpoint, schema,
                                      rules_fingerprint, False, seq)

        return self._run(op)

    # ---- 批准与基线 -----------------------------------------------

    def approve_and_promote(self, version_id: str) -> SnapshotRecord:
        """批准版本并将其提升为基线，单事务完成。

        与并发回滚/批准互斥（同一把锁 + 同一事务），结果确定：
        后提交者覆盖先提交者，基线永远指向一个已提交的完整版本。
        """

        def op() -> SnapshotRecord:
            with self._conn:
                row = self._conn.execute(
                    "SELECT * FROM versions WHERE version_id = ?", (version_id,)
                ).fetchone()
                if row is None:
                    raise ContractError(
                        FailureKind.VERSION_NOT_FOUND, f"版本不存在: {version_id}"
                    )
                self._conn.execute(
                    "UPDATE versions SET approved = 1 WHERE version_id = ?",
                    (version_id,),
                )
                self._conn.execute(
                    "INSERT INTO baselines (endpoint, version_id) VALUES (?, ?)"
                    " ON CONFLICT(endpoint) DO UPDATE SET version_id = excluded.version_id",
                    (row[1], version_id),
                )
                return self._row_to_record(
                    self._conn.execute(
                        "SELECT * FROM versions WHERE version_id = ?", (version_id,)
                    ).fetchone()
                )

        return self._run(op)

    def get_baseline(self, endpoint: str) -> SnapshotRecord | None:
        """读取某接口当前基线；无基线返回 None。读到的必是完整已提交版本。"""

        def op() -> SnapshotRecord | None:
            row = self._conn.execute(
                "SELECT v.* FROM baselines b JOIN versions v"
                " ON v.version_id = b.version_id WHERE b.endpoint = ?",
                (endpoint,),
            ).fetchone()
            return self._row_to_record(row) if row else None

        return self._run(op)

    def get_version(self, version_id: str) -> SnapshotRecord | None:
        def op() -> SnapshotRecord | None:
            row = self._conn.execute(
                "SELECT * FROM versions WHERE version_id = ?", (version_id,)
            ).fetchone()
            return self._row_to_record(row) if row else None

        return self._run(op)

    def list_versions(self, endpoint: str, limit: int = 100) -> list[SnapshotRecord]:
        """按创建序号倒序列出版本；带 limit，不随历史总量线性退化。"""

        def op() -> list[SnapshotRecord]:
            rows = self._conn.execute(
                "SELECT * FROM versions WHERE endpoint = ?"
                " ORDER BY created_seq DESC LIMIT ?",
                (endpoint, limit),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

        return self._run(op)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
