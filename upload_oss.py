"""
OSS 批量上传脚本
- 源目录中所有图片 → 阿里云 OSS bucket `img-dao` (cn-shanghai)
- key = 原始文件名（保留中文 / 特殊字符）
- SQLite 清单：断点续传 + 去重 + 失败追踪
- 多线程并发 + 指数退避重试
- 大文件断点续传
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import oss2
from tqdm import tqdm

# ========================= 配置 =========================
def _load_oss_md():
    """从同目录 oss.md 读取凭证（oss.md 已被 .gitignore 忽略，仅本地使用）。
    文件格式：每行 `key: value`，例如：
        accessKeyId: LTAI...
        accessKeySecret: ...
    """
    p = Path(__file__).resolve().parent / "oss.md"
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out

_oss_md = _load_oss_md()
# 优先级：环境变量 > 本地 oss.md
ACCESS_KEY_ID = os.environ.get("OSS_AK") or _oss_md.get("accessKeyId", "")
ACCESS_KEY_SECRET = os.environ.get("OSS_SK") or _oss_md.get("accessKeySecret", "")
ENDPOINT = os.environ.get("OSS_ENDPOINT") or _oss_md.get("endpoint") or "oss-cn-shanghai.aliyuncs.com"
BUCKET = os.environ.get("OSS_BUCKET") or _oss_md.get("bucketName", "img-dao")

SRC_DIR = Path(r"C:\Obsidian\assets\Images\Defalut")
DB_PATH = Path(__file__).resolve().parent / "oss_upload_manifest.db"
FAILED_LOG = Path(__file__).resolve().parent / "oss_upload_failed.txt"

# 图片扩展名（按需增减）
IMG_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".tiff", ".tif", ".svg", ".ico", ".avif", ".heic",
}

THREADS = 8
MAX_RETRIES = 5
HASH_THRESHOLD = 0          # >0 字节才计算 sha256（这里全部计算）
MULTIPART_THRESHOLD = 50 * 1024 * 1024  # 50MB 以上走断点续传
PART_SIZE = 5 * 1024 * 1024
# =======================================================


# ---------- DB ----------
def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            filename     TEXT PRIMARY KEY,   -- 同源目录下唯一
            local_path   TEXT NOT NULL,
            size         INTEGER NOT NULL,
            mtime        REAL NOT NULL,
            sha256       TEXT,
            oss_key      TEXT NOT NULL,
            status       TEXT NOT NULL,      -- pending / done / failed
            attempts     INTEGER NOT NULL DEFAULT 0,
            last_error   TEXT,
            uploaded_at  REAL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON files(status);")
    return conn


# ---------- 文件扫描 ----------
def iter_files(src: Path):
    for p in src.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMG_EXTS and p.stat().st_size > 0:
            yield p


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(conn: sqlite3.Connection, src: Path) -> int:
    """扫描目录，新增 / 更新清单。返回 pending 行数。"""
    cur = conn.cursor()
    inserted = updated = 0
    for p in tqdm(list(iter_files(src)), desc="扫描", unit="file"):
        st = p.stat()
        filename = p.name
        oss_key = p.relative_to(src).as_posix()  # 子目录则保留结构；平铺时 = filename
        row = cur.execute(
            "SELECT size, mtime, status FROM files WHERE filename=?", (filename,)
        ).fetchone()
        if row is None:
            cur.execute(
                """INSERT INTO files
                   (filename, local_path, size, mtime, sha256, oss_key, status)
                   VALUES (?,?,?,?,NULL,?,'pending')""",
                (filename, str(p), st.st_size, st.st_mtime, oss_key),
            )
            inserted += 1
        else:
            size, mtime, status = row
            if size == st.st_size and mtime == st.st_mtime and status == "done":
                continue  # 没变且已上传
            # 大小或修改时间变了，或者还是 pending/failed → 重算 hash 并标记 pending
            digest = sha256_of(p) if st.st_size > 0 else ""
            cur.execute(
                """UPDATE files
                   SET local_path=?, size=?, mtime=?, sha256=?, status='pending', last_error=NULL
                   WHERE filename=?""",
                (str(p), st.st_size, st.st_mtime, digest, filename),
            )
            if status == "done":
                updated += 1
    conn.commit()
    print(f"[scan] 新增 {inserted} 条，需重新上传 {updated} 条（之前已 done 但本地变化）")
    pending = cur.execute("SELECT COUNT(*) FROM files WHERE status='pending'").fetchone()[0]
    return pending


# ---------- 上传 ----------
def upload_one(bucket: oss2.Bucket, row: sqlite3.Row) -> tuple[bool, str | None]:
    filename, local_path, oss_key, size = (
        row["filename"], row["local_path"], row["oss_key"], row["size"]
    )
    path = Path(local_path)
    if not path.exists():
        return False, f"local file missing: {local_path}"

    if size == 0:
        return False, "skip 0-byte file"
    err: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if size >= MULTIPART_THRESHOLD:
                oss2.resumable_upload(
                    bucket, oss_key, str(path),
                    store=oss2.ResumableStore(root=Path(__file__).resolve().parent / ".oss_store"),
                    multipart_threshold=MULTIPART_THRESHOLD,
                    part_size=PART_SIZE,
                    num_threads=min(4, THREADS),
                )
            else:
                bucket.put_object_from_file(oss_key, str(path))
            return True, None
        except oss2.exceptions.OSS2Error as e:
            err = f"{type(e).__name__}: {getattr(e,'status','')} {getattr(e,'code','')} {getattr(e,'message','')}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        # 退避（最后一次不睡）
        if attempt < MAX_RETRIES:
            time.sleep(2 ** (attempt - 1))
    return False, err


def run(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT * FROM files WHERE status='pending' ORDER BY size ASC"
    ).fetchall()
    total = len(rows)
    if total == 0:
        print("没有待上传文件。")
        return

    auth = oss2.Auth(ACCESS_KEY_ID, ACCESS_KEY_SECRET)
    bucket = oss2.Bucket(auth, ENDPOINT, BUCKET, connect_timeout=30)
    # 探测连通性
    try:
        meta = bucket.get_bucket_info()
        print(f"[ok] 连接 bucket: {meta.name}, region: {meta.location}")
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] 无法访问 bucket：{e}")
        sys.exit(1)

    print(f"[run] 待上传 {total} 文件，{THREADS} 线程，最多 {MAX_RETRIES} 次重试")

    done = failed = skipped = 0
    failed_items: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=THREADS) as pool, \
         tqdm(total=total, desc="上传", unit="file") as bar:
        future_map = {pool.submit(upload_one, bucket, r): r for r in rows}
        for fut in as_completed(future_map):
            r = future_map[fut]
            ok, err = fut.result()
            now = time.time()
            if ok:
                conn.execute(
                    """UPDATE files
                       SET status='done', attempts=attempts+1, uploaded_at=?, last_error=NULL
                       WHERE filename=?""",
                    (now, r["filename"]),
                )
                done += 1
            else:
                conn.execute(
                    """UPDATE files
                       SET status='failed', attempts=attempts+1, last_error=?
                       WHERE filename=?""",
                    (err, r["filename"]),
                )
                failed += 1
                failed_items.append((r["filename"], err or ""))
            bar.set_postfix(done=done, failed=failed)
            bar.update(1)
    conn.commit()

    print("\n========== 汇总 ==========")
    print(f"成功 : {done}")
    print(f"失败 : {failed}")
    print(f"跳过 : {skipped}")

    if failed_items:
        FAILED_LOG.write_text(
            "\n".join(f"{n}\t{e}" for n, e in failed_items),
            encoding="utf-8",
        )
        print(f"失败列表已写入 {FAILED_LOG}")
        print("重新运行脚本即可自动重试 failed 项。")


# ---------- CLI ----------
def main():
    parser = argparse.ArgumentParser(description="批量上传图片到阿里云 OSS")
    parser.add_argument("--scan-only", action="store_true", help="只扫描入库，不上传")
    parser.add_argument("--rescan", action="store_true",
                        help="忽略缓存 mtime，全部重算 hash 并标 pending")
    parser.add_argument("--retry-failed", action="store_true",
                        help="把 failed 项重置为 pending 后重试")
    args = parser.parse_args()

    conn = init_db(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.rescan:
        conn.execute("UPDATE files SET status='pending', sha256=NULL")
        conn.commit()
        print("[reset] 全部标记为 pending")

    if args.retry_failed:
        n = conn.execute(
            "UPDATE files SET status='pending' WHERE status='failed'"
        ).rowcount
        conn.commit()
        print(f"[reset] {n} 个 failed 项重置为 pending")

    pending = scan(conn, SRC_DIR)
    print(f"[scan] 当前 pending: {pending}")

    if args.scan_only:
        return
    run(conn)


if __name__ == "__main__":
    main()
