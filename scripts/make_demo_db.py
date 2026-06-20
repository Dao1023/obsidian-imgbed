"""
生成 examples/sample_manifest.db —— dashboard 在线 demo 用的脱敏样本数据库。

固定随机种子，输出可复现。包含：
- 20 张假图（fake filenames + 假 sha256 + 假大小）
- 部分 image 被一些 fake md 引用（建 refs 表）
- 自动创建 ref_summary 视图（与 build_refs.py 保持一致）

用法：
    uv run scripts/make_demo_db.py
    # 或：python scripts/make_demo_db.py
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "examples" / "sample_manifest.db"

IMG_EXTS = [".png", ".jpg", ".gif", ".webp", ".svg"]
NOTE_TOPICS = [
    "ProjectA/design", "ProjectA/notes", "ProjectB/architecture",
    "Daily/2026-01", "Daily/2026-02", "Inbox", "Refs/books",
    "Learning/rust", "Learning/ai", "Travel/japan",
]


def fake_sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
        # 清理 WAL/SHM 残留
        for suffix in ("-wal", "-shm"):
            p = OUT.with_name(OUT.name + suffix)
            if p.exists():
                p.unlink()

    random.seed(42)  # 固定种子，可复现
    conn = sqlite3.connect(OUT)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE files (
            filename     TEXT PRIMARY KEY,
            local_path   TEXT NOT NULL,
            size         INTEGER NOT NULL,
            mtime        REAL NOT NULL,
            sha256       TEXT,
            oss_key      TEXT NOT NULL,
            status       TEXT NOT NULL,
            attempts     INTEGER NOT NULL DEFAULT 0,
            last_error   TEXT,
            uploaded_at  REAL
        )
        """
    )
    conn.execute("CREATE INDEX idx_status ON files(status);")

    conn.execute(
        """
        CREATE TABLE refs (
            image_filename TEXT NOT NULL,
            md_path        TEXT NOT NULL,
            PRIMARY KEY (image_filename, md_path)
        )
        """
    )

    now = time.time()
    files_buf = []
    for i in range(1, 21):
        ext = random.choice(IMG_EXTS)
        # 一些文件名形态贴近 Obsidian 真实使用
        kind = random.choice(["pasted", "screenshot", "named"])
        if kind == "pasted":
            filename = f"Pasted image 2026011{i:02d}{random.randint(10,59):02d}{random.randint(10,59):02d}{ext}"
        elif kind == "screenshot":
            filename = f"Screenshot 2026-01-{i:02d} at 10.{random.randint(10,59):02d}.{random.randint(10,59):02d}{ext}"
        else:
            filename = f"diagram-{i:02d}{ext}"
        size = random.randint(80_000, 6_000_000)
        oss_key = f"samples/{filename}"
        files_buf.append(
            (filename, f"/fake/assets/{filename}", size, now - random.randint(0, 10_000_000),
             fake_sha(filename), oss_key, "done", 1, None, now - random.randint(0, 1_000_000))
        )

    conn.executemany(
        "INSERT INTO files(filename, local_path, size, mtime, sha256, oss_key, status, attempts, last_error, uploaded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        files_buf,
    )

    # 给前 12 张图随机分配 md 引用，部分孤儿图
    refs_buf = []
    for filename, *_ in files_buf[:12]:
        # 每张图被 1-4 个 md 引用
        n_refs = random.randint(1, 4)
        picked = random.sample(NOTE_TOPICS, n_refs)
        for topic in picked:
            md = f"{topic}/note-{random.randint(1, 50):02d}.md"
            refs_buf.append((filename, md))
    conn.executemany(
        "INSERT OR IGNORE INTO refs(image_filename, md_path) VALUES (?,?)",
        refs_buf,
    )

    # 视图与 build_refs.py 一致
    conn.execute("DROP VIEW IF EXISTS ref_summary;")
    conn.execute(
        """
        CREATE VIEW ref_summary AS
        SELECT
            f.filename                                AS filename,
            f.size                                    AS size,
            f.oss_key                                 AS oss_key,
            COUNT(r.md_path)                          AS ref_count,
            GROUP_CONCAT(r.md_path, '|')              AS referenced_by
        FROM files f
        LEFT JOIN refs r ON r.image_filename = f.filename
        GROUP BY f.filename
        """
    )
    conn.commit()
    conn.close()

    # 统计
    conn = sqlite3.connect(OUT)
    n_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    n_refs = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
    n_orphan = conn.execute(
        "SELECT COUNT(*) FROM files f WHERE NOT EXISTS (SELECT 1 FROM refs r WHERE r.image_filename=f.filename)"
    ).fetchone()[0]
    conn.close()

    print(f"[ok] wrote {OUT}")
    print(f"     files: {n_files}, refs: {n_refs}, orphans: {n_orphan}")
    print(f"     size: {OUT.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
