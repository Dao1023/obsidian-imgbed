"""
将 Obsidian Markdown 中的图片 wikilink 替换为图床直链 —— obsidian-imgbed

输入：
  - manifest DB（图片上传记录，filename → oss_key）
  - Markdown 根目录（递归扫描所有 .md）

规则：
  [[xxx.png]]      → ![](<url>)
  ![[xxx.png]]     → ![](<url>)
  [[xxx.png|opts]] → ![](<url>)   (丢弃 | 后内容)
  ![](local/path)  → ![](<url>)   (本地相对路径也尝试 basename 匹配)

非图片扩展名 / DB 未命中 → 原样保留。

用法：
    uv run replace_links.py --md-dir /path/to/md --db manifest.db \\
        --url-prefix https://bucket.oss-cn-shanghai.aliyuncs.com/ --dry-run
    # 确认无误后加 --apply 才写盘
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path, PurePath
from urllib.parse import quote

# 可选：自动加载 .env
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    pass

# 这些扩展名才视为“图片”，参与替换
IMG_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".svg", ".ico", ".tiff", ".tif", ".avif", ".heic",
}


def load_db(db_path: Path) -> dict[str, str]:
    """返回 {filename_lower: oss_key}，只含 status='done'"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT filename, oss_key FROM files WHERE status='done'"
    ).fetchall()
    conn.close()
    return {name.lower(): key for name, key in rows}


def make_url_builder(url_prefix: str):
    """返回一个把 oss_key 转成最终 markdown URL 的函数。"""
    # 确保 prefix 以 / 结尾
    prefix = url_prefix if url_prefix.endswith("/") else url_prefix + "/"
    def build_url(oss_key: str) -> str:
        encoded = quote(oss_key, safe="/")
        return f"<{prefix}{encoded}>"
    return build_url


# wikilink: [[...]] 可选前缀 !，内部可带 |opts
WIKI_RE = re.compile(r"(!?)\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")
# 标准 markdown 图片: ![alt](path)
MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def is_image_name(name: str) -> bool:
    return PurePath(name).suffix.lower() in IMG_EXTS


def replace_text(
    text: str,
    db: dict[str, str],
    build_url,
    stats: Counter,
    miss_buf: list[str],
    src_file: str,
) -> str:
    def wiki_sub(m: re.Match) -> str:
        target = m.group(2).strip()
        base = PurePath(target).name
        if not is_image_name(base):
            return m.group(0)
        key = db.get(base.lower())
        if key is None:
            stats["miss"] += 1
            miss_buf.append(f"{src_file}\t{base}")
            return m.group(0)
        stats["replaced_wikilink"] += 1
        return f"![]({build_url(key)})"

    def md_img_sub(m: re.Match) -> str:
        alt, path = m.group(1), m.group(2).strip()
        if path.startswith(("http://", "https://", "<")):
            return m.group(0)
        base = PurePath(path).name
        if not is_image_name(base):
            return m.group(0)
        key = db.get(base.lower())
        if key is None:
            stats["miss"] += 1
            miss_buf.append(f"{src_file}\t{base} (mdlink)")
            return m.group(0)
        stats["replaced_mdlink"] += 1
        return f"![{alt}]({build_url(key)})"

    text = WIKI_RE.sub(wiki_sub, text)
    text = MD_IMG_RE.sub(md_img_sub, text)
    return text


def main():
    ap = argparse.ArgumentParser(
        description="将 Obsidian Markdown 中的图片 wikilink 替换为图床直链（obsidian-imgbed）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--md-dir", default=os.environ.get("OBSIDIAN_MD_DIR"),
                    help="Obsidian Markdown 根目录（递归扫描）")
    ap.add_argument("--db", default=os.environ.get("OSS_MANIFEST_DB", "manifest.db"),
                    help="SQLite 清单路径")
    ap.add_argument("--url-prefix", default=os.environ.get("OSS_URL_PREFIX"),
                    help="图床公开 URL 前缀，例如 https://bucket.oss-cn-shanghai.aliyuncs.com/")
    ap.add_argument("--apply", action="store_true",
                    help="默认 dry-run，加此参数才真正写文件")
    ap.add_argument("--dry-run", action="store_true",
                    help="显式指定 dry-run（默认即此模式）")
    ap.add_argument("--miss-log", type=Path, default=None,
                    help="未命中图片引用列表输出位置（默认与 db 同目录的 *_replace_miss.txt）")
    args = ap.parse_args()

    if not args.md_dir:
        ap.error("--md-dir 必填（或设置 OBSIDIAN_MD_DIR）")
    if not args.url_prefix:
        ap.error("--url-prefix 必填（或设置 OSS_URL_PREFIX）")

    src_md_dir = Path(args.md_dir)
    if not src_md_dir.is_dir():
        ap.error(f"md-dir 不是目录: {src_md_dir}")

    db_path = Path(args.db)
    miss_log = args.miss_log or db_path.with_name(db_path.stem + "_replace_miss.txt")

    db = load_db(db_path)
    print(f"[load] DB 命中图片记录: {len(db)}")

    md_files = list(src_md_dir.rglob("*.md"))
    md_files = [
        p for p in md_files
        if not any(part.startswith(".") for part in p.relative_to(src_md_dir).parts)
    ]
    print(f"[scan] Markdown 文件: {len(md_files)}")

    build_url = make_url_builder(args.url_prefix)
    stats: Counter = Counter()
    miss_buf: list[str] = []

    for p in md_files:
        try:
            orig = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            orig = p.read_text(encoding="utf-8", errors="replace")
        new = replace_text(orig, db, build_url, stats, miss_buf, str(p.relative_to(src_md_dir)))
        if new != orig:
            if args.apply:
                stats["changed_files"] += 1
                p.write_text(new, encoding="utf-8")
            else:
                stats["would_change_files"] += 1

    if miss_buf:
        miss_log.write_text("\n".join(miss_buf), encoding="utf-8")
    else:
        miss_log.unlink(missing_ok=True)

    print()
    print("========== 统计 ==========")
    print(f"模式                : {'APPLY (写盘)' if args.apply else 'DRY-RUN (只读)'}")
    print(f"扫描 .md            : {len(md_files)}")
    print(f"{'将被修改的文件' if not args.apply else '已修改的文件':<20}: {stats['changed_files'] if args.apply else stats['would_change_files']}")
    print(f"wikilink 替换次数   : {stats['replaced_wikilink']}")
    print(f"markdown 链接替换   : {stats['replaced_mdlink']}")
    print(f"未命中（保留原样） : {stats['miss']}")
    print(f"未命中列表          : {miss_log if miss_buf else '（无）'}")

    if not args.apply:
        print()
        print(f"确认无误后运行: uv run replace_links.py --md-dir ... --db ... --url-prefix ... --apply")


if __name__ == "__main__":
    main()
