"""
OSS 批量上传 CLI —— obsidian-imgbed

把源目录下所有图片批量上传到阿里云 OSS bucket，写入 SQLite 清单。
支持断点续传、多线程并发、指数退避重试、≥50MB 大文件分片上传。

配置优先级（高 → 低）：
    CLI 参数 > 环境变量 / .env > 脚本同目录 oss.md（仅本地）

用法示例：
    uv run upload_oss.py --src /path/to/images --bucket my-bucket \\
        --access-key LTAI... --access-secret s7...
    # 或：cp .env.example .env 填好变量，再 uv run upload_oss.py
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import oss2
from tqdm import tqdm

# 可选：自动加载 .env
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    pass


# ========================= 常量 =========================
IMG_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".tiff", ".tif", ".svg", ".ico", ".avif", ".heic",
}

DEFAULT_ENDPOINT = "oss-cn-shanghai.aliyuncs.com"
DEFAULT_THREADS = 8
MAX_RETRIES = 5
HASH_THRESHOLD = 0          # >0 字节才计算 sha256（0 表示全部计算）
MULTIPART_THRESHOLD = 50 * 1024 * 1024  # 50MB 以上走断点续传
PART_SIZE = 5 * 1024 * 1024


def _load_oss_md() -> dict:
    """从脚本同目录 oss.md 读取凭证（oss.md 已被 .gitignore，仅本地使用）。
    文件格式：每行 `key: value`。
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


# ---------- DB ----------
def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
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
        oss_key = p.relative_to(src).as_posix()
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
                continue
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
def upload_one(bucket: oss2.Bucket, row: sqlite3.Row, threads: int,
               resumable_root: Path) -> tuple[bool, str | None]:
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
                    store=oss2.ResumableStore(root=resumable_root),
                    multipart_threshold=MULTIPART_THRESHOLD,
                    part_size=PART_SIZE,
                    num_threads=min(4, threads),
                )
            else:
                bucket.put_object_from_file(oss_key, str(path))
            return True, None
        except oss2.exceptions.OSS2Error as e:
            err = f"{type(e).__name__}: {getattr(e,'status','')} {getattr(e,'code','')} {getattr(e,'message','')}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        if attempt < MAX_RETRIES:
            time.sleep(2 ** (attempt - 1))
    return False, err


def run(conn: sqlite3.Connection, *, ak: str, sk: str, endpoint: str, bucket_name: str,
        threads: int, failed_log: Path, resumable_root: Path):
    rows = conn.execute(
        "SELECT * FROM files WHERE status='pending' ORDER BY size ASC"
    ).fetchall()
    total = len(rows)
    if total == 0:
        print("没有待上传文件。")
        return

    auth = oss2.Auth(ak, sk)
    bucket = oss2.Bucket(auth, endpoint, bucket_name, connect_timeout=30)
    try:
        meta = bucket.get_bucket_info()
        print(f"[ok] 连接 bucket: {meta.name}, region: {meta.location}")
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] 无法访问 bucket：{e}")
        sys.exit(1)

    print(f"[run] 待上传 {total} 文件，{threads} 线程，最多 {MAX_RETRIES} 次重试")

    done = failed = 0
    failed_items: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=threads) as pool, \
         tqdm(total=total, desc="上传", unit="file") as bar:
        future_map = {pool.submit(upload_one, bucket, r, threads, resumable_root): r for r in rows}
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

    if failed_items:
        failed_log.write_text(
            "\n".join(f"{n}\t{e}" for n, e in failed_items),
            encoding="utf-8",
        )
        print(f"失败列表已写入 {failed_log}")
        print("重新运行脚本即可自动重试 failed 项。")


# ---------- CLI ----------
def main():
    parser = argparse.ArgumentParser(
        description="批量上传图片到阿里云 OSS（obsidian-imgbed）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--src", default=os.environ.get("OBSIDIAN_IMG_DIR"),
                        help="图片源目录（递归扫描）")
    parser.add_argument("--db", default=os.environ.get("OSS_MANIFEST_DB", "manifest.db"),
                        help="SQLite 清单路径")
    parser.add_argument("--bucket", default=os.environ.get("OSS_BUCKET"),
                        help="OSS bucket 名")
    parser.add_argument("--endpoint", default=os.environ.get("OSS_ENDPOINT", DEFAULT_ENDPOINT),
                        help="OSS endpoint")
    parser.add_argument("--access-key", default=os.environ.get("OSS_AK"),
                        help="AccessKey ID（也可用 oss.md 或 .env）")
    parser.add_argument("--access-secret", default=os.environ.get("OSS_SK"),
                        help="AccessKey Secret（也可用 oss.md 或 .env）")
    parser.add_argument("--threads", type=int,
                        default=int(os.environ.get("UPLOAD_THREADS", str(DEFAULT_THREADS))),
                        help="并发线程数")
    parser.add_argument("--scan-only", action="store_true", help="只扫描入库，不上传")
    parser.add_argument("--rescan", action="store_true",
                        help="忽略缓存 mtime，全部重算 hash 并标 pending")
    parser.add_argument("--retry-failed", action="store_true",
                        help="把 failed 项重置为 pending 后重试")
    args = parser.parse_args()

    # 校验必填
    if not args.src:
        parser.error("--src 必填（或设置 OBSIDIAN_IMG_DIR 环境变量）")
    if not args.bucket:
        parser.error("--bucket 必填（或设置 OSS_BUCKET）")

    src = Path(args.src)
    if not src.is_dir():
        parser.error(f"src 不是目录: {src}")

    # AK/SK 兜底：CLI/env → oss.md
    oss_md = _load_oss_md()
    ak = args.access_key or oss_md.get("accessKeyId")
    sk = args.access_secret or oss_md.get("accessKeySecret")
    if not ak or not sk:
        parser.error("AccessKey 缺失：传 --access-key/--access-secret，或设置 OSS_AK/OSS_SK，或在脚本同目录放 oss.md")

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    failed_log = db_path.with_name(db_path.stem + "_failed.txt")
    resumable_root = db_path.parent / ".oss_store"

    conn = init_db(db_path)
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

    pending = scan(conn, src)
    print(f"[scan] 当前 pending: {pending}")

    if args.scan_only:
        return

    run(conn, ak=ak, sk=sk, endpoint=args.endpoint, bucket_name=args.bucket,
        threads=args.threads, failed_log=failed_log, resumable_root=resumable_root)


if __name__ == "__main__":
    main()
