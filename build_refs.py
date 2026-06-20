"""
扫描所有 .md，统计每张图片被哪些笔记引用 —— obsidian-imgbed

数据源：
  - 仍残留的 wikilink：[[xxx.png]] / ![[xxx.png]]
  - 已被替换的图床直链：![](<https://bucket...aliyuncs.com/xxx>)
  - 极少见的本地 md 图片链接（未替换的）

输出：
  1. SQLite 关系表 refs(image_filename, md_path) — 规范化、可任意 JOIN
  2. 视图 ref_summary(filename, size, oss_key, ref_count, referenced_by)
  3. CSV：filename, size, ref_count, md_files（| 分隔）

用法：
    uv run build_refs.py --md-dir /path/to/md --db manifest.db \\
        --url-prefix https://bucket.oss-cn-shanghai.aliyuncs.com/ \\
        --csv-out refs.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
from pathlib import Path, PurePath
from urllib.parse import unquote

# 可选：自动加载 .env
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    pass

IMG_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".svg", ".ico", ".tiff", ".tif", ".avif", ".heic",
}

# 匹配两种形态
WIKI_RE = re.compile(r"(!?)\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")
MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(<([^>]+)>\)")
MD_IMG_LOCAL_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def main():
    ap = argparse.ArgumentParser(
        description="扫描 Markdown 笔记，构建图片反向引用表（obsidian-imgbed）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--md-dir", default=os.environ.get("OBSIDIAN_MD_DIR"),
                    help="Obsidian Markdown 根目录（递归扫描）")
    ap.add_argument("--db", default=os.environ.get("OSS_MANIFEST_DB", "manifest.db"),
                    help="SQLite 清单路径（写入 refs 表与 ref_summary 视图）")
    ap.add_argument("--url-prefix", default=os.environ.get("OSS_URL_PREFIX"),
                    help="图床公开 URL 前缀（用于识别已被替换的图床直链）")
    ap.add_argument("--csv-out", default=None,
                    help="CSV 输出路径（默认与 db 同目录的 *_refs.csv）")
    args = ap.parse_args()

    if not args.md_dir:
        ap.error("--md-dir 必填（或设置 OBSIDIAN_MD_DIR）")
    if not args.url_prefix:
        ap.error("--url-prefix 必填（或设置 OSS_URL_PREFIX）")

    src_md_dir = Path(args.md_dir)
    if not src_md_dir.is_dir():
        ap.error(f"md-dir 不是目录: {src_md_dir}")

    db_path = Path(args.db)
    csv_out = Path(args.csv_out) if args.csv_out else db_path.with_name(db_path.stem + "_refs.csv")
    url_prefix = args.url_prefix if args.url_prefix.endswith("/") else args.url_prefix + "/"

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")

    # oss_key → filename 的反查表（含大小写不敏感）
    rows = conn.execute(
        "SELECT filename, oss_key FROM files WHERE status='done'"
    ).fetchall()
    key_to_name = {key.lower(): name for name, key in rows}
    name_set = {name.lower() for name, _ in rows}
    print(f"[load] DB 图片: {len(rows)}")

    # 建关系表
    conn.execute("DROP TABLE IF EXISTS refs;")
    conn.execute(
        """
        CREATE TABLE refs (
            image_filename TEXT NOT NULL,
            md_path        TEXT NOT NULL,
            PRIMARY KEY (image_filename, md_path)
        )
        """
    )

    md_files = [
        p for p in src_md_dir.rglob("*.md")
        if not any(part.startswith(".")
                   for part in p.relative_to(src_md_dir).parts)
    ]
    print(f"[scan] Markdown: {len(md_files)}")

    insert_buf: list[tuple[str, str]] = []
    scanned_refs = 0
    files_with_ref = 0

    for p in md_files:
        rel = p.relative_to(src_md_dir).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="utf-8", errors="replace")

        hits: set[str] = set()

        # 1) 残留 wikilink
        for m in WIKI_RE.finditer(text):
            base = PurePath(m.group(2).strip()).name
            if PurePath(base).suffix.lower() not in IMG_EXTS:
                continue
            if base.lower() in name_set:
                hits.add(base)

        # 2) 已替换的图床 URL
        for m in MD_IMG_RE.finditer(text):
            url = m.group(1)
            if not url.startswith(url_prefix):
                continue
            oss_key = unquote(url[len(url_prefix):])
            name = key_to_name.get(oss_key.lower())
            if name:
                hits.add(name)

        # 3) 极少见的本地 md 图片链接（未替换的）
        for m in MD_IMG_LOCAL_RE.finditer(text):
            path = m.group(1).strip()
            if path.startswith(("http://", "https://", "<")):
                continue
            base = PurePath(path).name
            if PurePath(base).suffix.lower() not in IMG_EXTS:
                continue
            if base.lower() in name_set:
                hits.add(base)

        if hits:
            files_with_ref += 1
            for h in hits:
                insert_buf.append((h, rel))
                scanned_refs += 1

    conn.executemany(
        "INSERT OR IGNORE INTO refs(image_filename, md_path) VALUES (?,?)",
        insert_buf,
    )
    conn.commit()

    # 视图：每张图片的引用汇总
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

    # 导出 CSV（仅被引用过的）
    rows = conn.execute(
        """
        SELECT filename, size, ref_count, referenced_by
        FROM ref_summary
        WHERE ref_count > 0
        ORDER BY ref_count DESC, filename
        """
    ).fetchall()
    with csv_out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["filename", "size", "ref_count", "md_files"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], r[3]])

    # 统计
    total_imgs = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    imgs_referenced = conn.execute(
        "SELECT COUNT(DISTINCT image_filename) FROM refs"
    ).fetchone()[0]
    md_referencing = conn.execute(
        "SELECT COUNT(DISTINCT md_path) FROM refs"
    ).fetchone()[0]
    top5 = conn.execute(
        """
        SELECT filename, ref_count
        FROM ref_summary
        ORDER BY ref_count DESC LIMIT 5
        """
    ).fetchall()

    print()
    print("========== 引用统计 ==========")
    print(f"图片总数              : {total_imgs}")
    print(f"被引用的图片          : {imgs_referenced}  ({imgs_referenced*100/total_imgs:.1f}%)")
    print(f"完全未被引用的        : {total_imgs - imgs_referenced}")
    print(f"引用过图片的 md       : {md_referencing} / {len(md_files)}")
    print(f"引用记录总数（含重复）: {scanned_refs}")
    print()
    print("引用量 Top 5:")
    for name, n in top5:
        print(f"  {n:>4}  {name}")
    print()
    print(f"CSV 已导出: {csv_out}")
    print()
    print("查询示例:")
    print(f"  sqlite3 {db_path.name} \\")
    print('    "SELECT md_path FROM refs WHERE image_filename=\'xxx.png\'"')


if __name__ == "__main__":
    main()
