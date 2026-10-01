"""SQLite 连接管理与初始化。

使用 WAL 并开启外键约束；写操作通过 ``immediate_tx`` 先获取保留锁，
配合条件 UPDATE 保证「同一物品并发确认最多成功一次」。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path), timeout=10, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """开启 IMMEDIATE 事务：进入即取写锁，避免并发写互相饿死。

    正常退出提交，异常回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(db_path: str | Path) -> None:
    """创建库文件并应用幂等建表脚本（即数据初始化），再执行结构升级。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect(path)
    try:
        conn.executescript(ddl)
        _migrate_items_merge_status(conn)
    finally:
        conn.close()


def _migrate_items_merge_status(conn: sqlite3.Connection) -> None:
    """为旧库的 items 增加 merged 状态取值与 merged_into_item_id 列。

    SQLite 无法直接修改 CHECK 约束，需在关闭外键的一次性脚本内重建表。
    新库的 schema 已含新列，PRAGMA 检查到列存在即跳过，升级幂等。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "merged_into_item_id" in cols:
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            """
            CREATE TABLE items_new (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                public_description TEXT    NOT NULL,
                private_features   TEXT    NOT NULL,
                status             TEXT    NOT NULL DEFAULT 'kept'
                                     CHECK (status IN ('kept', 'removed', 'returned', 'merged')),
                created_at         TEXT    NOT NULL,
                updated_at         TEXT    NOT NULL,
                merged_into_item_id INTEGER REFERENCES items(id)
            );
            INSERT INTO items_new
                (id, public_description, private_features, status, created_at, updated_at,
                 merged_into_item_id)
            SELECT id, public_description, private_features, status, created_at, updated_at, NULL
            FROM items;
            DROP TABLE items;
            ALTER TABLE items_new RENAME TO items;
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
