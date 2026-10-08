"""持久化存储：快照版本、基线指针、判定记录。

设计要点：
- 默认使用进程工作目录下的单文件 SQLite 库（见 default_db_path），
  重启进程后历史快照、基线与判定记录仍在；显式传入路径或设置
  环境变量 CONTRACT_SNAPSHOT_DB 可覆盖，``:memory:`` 用于测试；
- SQLite 单文件存储，文件库启用 WAL 支持并发读写；
- 所有多步写入包在一个事务里，要么完整提交要么整体回滚，
  不会留下写了一半的中间产物；
- 基线（baseline）是单行指针，更新与读取都是原子的，
  任一时刻读到的都是某个已提交的完整版本；
- 判定记录独立成表：配置变更只影响之后写入的判定，
  已落库的历史判定依据永不被改写。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import ContractError, FailureKind

# 未显式配置持久化位置时的默认落盘目录与文件名（相对工作目录）。
DEFAULT_DATA_DIR = ".contract_snapshot"
DEFAULT_DB_FILENAME = "contracts.db"

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

CREATE TABLE IF NOT EXISTS judgments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT NOT NULL,
    version_id  TEXT NOT NULL REFERENCES versions(version_id),
    baseline_version_id TEXT,
    verdict     TEXT NOT NULL,
    rules_match INTEGER NOT NULL,
    created_seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judgments_endpoint
    ON judgments(endpoint, id);
"""


def default_db_path(data_dir: str | os.PathLike[str] | None = None) -> str:
    """返回默认落盘数据库路径：``$data_dir/contracts.db``。

    data_dir 为 None 时使用工作目录下的 .contract_snapshot/，
    也可由环境变量 CONTRACT_SNAPSHOT_DATA_DIR 覆盖。
    """
    base = data_dir or os.environ.get("CONTRACT_SNAPSHOT_DATA_DIR", DEFAULT_DATA_DIR)
    return str(Path(base) / DEFAULT_DB_FILENAME)


@dataclass(frozen=True)
class SnapshotRecord:
    """一条已固化的快照版本。"""

    version_id: str
    endpoint: str
    schema: dict
    rules_fingerprint: str
    approved: bool
    created_seq: int


@dataclass(frozen=True)
class JudgmentRecord:
    """一次采集的判定记录（落盘，配置变更不影响历史记录）。"""

    endpoint: str
    version_id: str
    baseline_version_id: str | None
    verdict: str
    rules_match: bool
    created_seq: int


class SnapshotStore:
    """快照与基线的持久化存储。线程安全。"""

    def __init__(self, db_path: str | None = None) -> None:
        self._lock = threading.RLock()
        # 默认落盘：未显式传入时使用工作目录下的默认库，重启后数据仍在。
        self._db_path = default_db_path() if db_path is None else db_path
        try:
            if self._db_path != ":memory:":
                parent = os.path.dirname(os.path.abspath(self._db_path))
                os.makedirs(parent, exist_ok=True)
            # check_same_thread=False + 显式锁：多线程共享一个连接。
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            if self._db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            with self._conn:
                self._conn.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            raise ContractError(
                FailureKind.STORE_UNAVAILABLE, f"存储初始化失败: {exc}"
            ) from exc

    @property
    def db_path(self) -> str:
        return self._db_path

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

    # ---- 判定记录 -------------------------------------------------

    def save_judgment(
        self,
        endpoint: str,
        version_id: str,
        baseline_version_id: str | None,
        verdict: str,
        rules_match: bool,
        created_seq: int,
    ) -> None:
        """固化一次采集的判定依据。历史记录只追加，永不被配置变更改写。"""

        def op() -> None:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO judgments"
                    " (endpoint, version_id, baseline_version_id, verdict,"
                    "  rules_match, created_seq)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (endpoint, version_id, baseline_version_id, verdict,
                     1 if rules_match else 0, created_seq),
                )

        self._run(op)

    def get_last_judgment(self, endpoint: str) -> JudgmentRecord | None:
        """读取该接口最近一次采集判定；无记录返回 None。"""

        def op() -> JudgmentRecord | None:
            row = self._conn.execute(
                "SELECT endpoint, version_id, baseline_version_id, verdict,"
                " rules_match, created_seq FROM judgments"
                " WHERE endpoint = ? ORDER BY id DESC LIMIT 1",
                (endpoint,),
            ).fetchone()
            if row is None:
                return None
            return JudgmentRecord(
                endpoint=row[0],
                version_id=row[1],
                baseline_version_id=row[2],
                verdict=row[3],
                rules_match=bool(row[4]),
                created_seq=row[5],
            )

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
