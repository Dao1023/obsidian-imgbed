"""
扫描所有 .md，统计每张图片被哪些笔记引用。

数据源：
  - 仍残留的 wikilink：[[xxx.png]] / ![[xxx.png]]
  - 已被替换的图床直链：![](<https://img-dao.oss-cn-shanghai.aliyuncs.com/xxx>)

输出：
  1. SQLite 关系表 refs(image_filename, md_path) — 规范化、可任意 JOIN
  2. 视图 ref_summary(filename, size, ref_count, referenced_by)
  3. CSV：filename, size, ref_count, md_files（| 分隔）
"""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
from pathlib import Path, PurePath
from urllib.parse import unquote

DB_PATH = Path(__file__).resolve().parent / "oss_upload_manifest.db"
SRC_MD_DIR = Path(r"C:\Obsidian\Markdown")
URL_PREFIX = "https://img-dao.oss-cn-shanghai.aliyuncs.com/"
CSV_OUT = Path(__file__).resolve().parent / "image_refs.csv"

IMG_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".svg", ".ico", ".tiff", ".tif", ".avif", ".heic",
}

# 匹配两种形态
WIKI_RE = re.compile(r"(!?)\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")
MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(<([^>]+)>\)")
MD_IMG_LOCAL_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def main():
    conn = sqlite3.connect(DB_PATH)
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
        p for p in SRC_MD_DIR.rglob("*.md")
        if not any(part.startswith(".")
                   for part in p.relative_to(SRC_MD_DIR).parts)
    ]
    print(f"[scan] Markdown: {len(md_files)}")

    insert_buf: list[tuple[str, str]] = []
    scanned_refs = 0
    files_with_ref = 0

    for p in md_files:
        rel = p.relative_to(SRC_MD_DIR).as_posix()
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
            if not url.startswith(URL_PREFIX):
                continue
            oss_key = unquote(url[len(URL_PREFIX):])
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
    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as f:
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
    print(f"CSV 已导出: {CSV_OUT}")
    print()
    print("查询示例:")
    print("  sqlite3 oss_upload_manifest.db \\")
    print('    "SELECT md_path FROM refs WHERE image_filename=\'xxx.png\'"')


if __name__ == "__main__":
    main()
