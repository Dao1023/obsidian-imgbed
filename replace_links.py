"""
将 Obsidian Markdown 中的图片 wikilink 替换为图床直链。

输入：
  - manifest DB（图片上传记录，filename → oss_key）
  - C:\\Obsidian\\Markdown 下所有 .md

规则：
  [[xxx.png]]      → ![](<url>)
  ![[xxx.png]]     → ![](<url>)
  [[xxx.png|opts]] → ![](<url>)   (丢弃 | 后内容)
  ![](local/path)  → ![](<url>)   (本地相对路径也尝试 basename 匹配)

非图片扩展名 / DB 未命中 → 原样保留。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path, PurePath
from urllib.parse import quote

# ============ 配置 ============
DB_PATH = Path(__file__).resolve().parent / "oss_upload_manifest.db"
SRC_MD_DIR = Path(r"C:\Obsidian\Markdown")
URL_PREFIX = "https://img-dao.oss-cn-shanghai.aliyuncs.com/"

# 这些扩展名才视为“图片”，参与替换
IMG_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".svg", ".ico", ".tiff", ".tif", ".avif", ".heic",
}
# =============================


def load_db(db_path: Path) -> dict[str, str]:
    """返回 {filename_lower: oss_key}，只含 status='done'"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT filename, oss_key FROM files WHERE status='done'"
    ).fetchall()
    conn.close()
    # 小写做 key，大小写不敏感匹配
    return {name.lower(): key for name, key in rows}


def build_url(oss_key: str) -> str:
    # quote 保留 /，整体用 <> 包裹，安全处理中文/特殊字符/空格/括号
    encoded = quote(oss_key, safe="/")
    return f"<{URL_PREFIX}{encoded}>"


# wikilink: [[...]] 可选前缀 !，内部可带 |opts
WIKI_RE = re.compile(r"(!?)\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")
# 标准 markdown 图片: ![alt](path)
MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def is_image_name(name: str) -> bool:
    return PurePath(name).suffix.lower() in IMG_EXTS


def replace_text(
    text: str,
    db: dict[str, str],
    stats: Counter,
    miss_buf: list[str],
    src_file: str,
) -> str:
    def wiki_sub(m: re.Match) -> str:
        target = m.group(2).strip()
        # 取 basename（Obsidian 允许 [[path/to/x.png]]）
        base = PurePath(target).name
        if not is_image_name(base):
            return m.group(0)  # 非图片，原样
        key = db.get(base.lower())
        if key is None:
            stats["miss"] += 1
            miss_buf.append(f"{src_file}\t{base}")
            return m.group(0)
        stats["replaced_wikilink"] += 1
        return f"![]({build_url(key)})"

    def md_img_sub(m: re.Match) -> str:
        alt, path = m.group(1), m.group(2).strip()
        # 仅处理本地路径（http 开头的不动）
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="默认 dry-run，加此参数才真正写文件")
    ap.add_argument("--miss-log", type=Path,
                    default=Path(__file__).resolve().parent / "oss_replace_miss.txt",
                    help="未命中图片引用列表输出位置")
    args = ap.parse_args()

    if not SRC_MD_DIR.exists():
        print(f"[fatal] 源目录不存在: {SRC_MD_DIR}", file=sys.stderr)
        sys.exit(1)

    db = load_db(DB_PATH)
    print(f"[load] DB 命中图片记录: {len(db)}")

    md_files = list(SRC_MD_DIR.rglob("*.md"))
    # 排除 .obsidian 等隐藏目录
    md_files = [
        p for p in md_files
        if not any(part.startswith(".") for part in p.relative_to(SRC_MD_DIR).parts)
    ]
    print(f"[scan] Markdown 文件: {len(md_files)}")

    stats: Counter = Counter()
    miss_buf: list[str] = []
    changed_files = 0

    for p in md_files:
        try:
            orig = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            orig = p.read_text(encoding="utf-8", errors="replace")
        new = replace_text(orig, db, stats, miss_buf, str(p.relative_to(SRC_MD_DIR)))
        if new != orig:
            changed_files += 1
            if args.apply:
                stats["changed_files"] += 1
                p.write_text(new, encoding="utf-8")
            else:
                stats["would_change_files"] += 1

    args.miss_log.write_text(
        "\n".join(miss_buf), encoding="utf-8"
    ) if miss_buf else args.miss_log.unlink(missing_ok=True)

    print()
    print("========== 统计 ==========")
    print(f"模式                : {'APPLY (写盘)' if args.apply else 'DRY-RUN (只读)'}")
    print(f"扫描 .md            : {len(md_files)}")
    print(f"{'将被修改的文件' if not args.apply else '已修改的文件':<20}: {stats['changed_files'] if args.apply else stats['would_change_files']}")
    print(f"wikilink 替换次数   : {stats['replaced_wikilink']}")
    print(f"markdown 链接替换   : {stats['replaced_mdlink']}")
    print(f"未命中（保留原样） : {stats['miss']}")
    print(f"未命中列表          : {args.miss_log if miss_buf else '（无）'}")

    if not args.apply:
        print()
        print("确认无误后运行: python replace_links.py --apply")


if __name__ == "__main__":
    main()
