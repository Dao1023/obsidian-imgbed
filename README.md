# obsidian-imgbed

> 把 Obsidian 本地图片批量迁移到阿里云 OSS，并把 Markdown 里的 wikilink 自动替换成图床直链，附带反向引用可视化 dashboard。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Made with uv](https://img.shields.io/badge/Made%20with-uv-blueviolet.svg)](https://github.com/astral-sh/uv)

## ✨ 特性

- 🚀 **批量上传**：递归扫描目录，多线程并发，断点续传，指数退避重试，≥50MB 自动分片
- 🔄 **链接替换**：`[[xxx.png]]` / `![[xxx.png]]` / `[[xxx.png|opts]]` / `![](local/path)` 一键替换为图床 URL
- 🔍 **反向引用**：扫描全部 md，构建「图片 ↔ 笔记」关系表，知道哪张图被哪篇笔记引用过
- 📊 **可视化 Dashboard**：单文件 HTML，拖入 `manifest.db` 即用，无需后端
- 🔐 **零外部依赖部署**：dashboard 是纯静态页面，可托管在 GitHub Pages

## 📸 截图

> _（待补：dashboard 4 个 tab + 详情 modal 截图）_

**[在线 demo](https://dao1023.github.io/obsidian-imgbed/)** —— 自动加载脱敏样本数据，可直接体验。

## 🚀 快速上手

### 1. 安装

```bash
git clone https://github.com/Dao1023/obsidian-imgbed.git
cd obsidian-imgbed
uv sync
```

> 没装 [uv](https://github.com/astral-sh/uv)？`curl -LsSf https://astral.sh/uv/install.sh | sh`。

### 2. 配置凭证

```bash
cp .env.example .env
# 编辑 .env，填入你的 OSS 信息和 Obsidian 路径
```

需要的最小配置：

```env
OBSIDIAN_IMG_DIR=/path/to/obsidian/assets/images
OBSIDIAN_MD_DIR=/path/to/obsidian/markdown
OSS_BUCKET=your-bucket
OSS_ENDPOINT=oss-cn-shanghai.aliyuncs.com
OSS_AK=your-access-key-id
OSS_SK=your-access-key-secret
OSS_URL_PREFIX=https://your-bucket.oss-cn-shanghai.aliyuncs.com/
```

> Bucket 需要开启**公共读**权限（这样 markdown 才能直接渲染图床 URL）。

### 3. 三条命令完成迁移

```bash
# ① 上传所有图片（断点续传，可重复运行）
uv run upload_oss.py

# ② 替换 md 中的 wikilink —— 先 dry-run 看看会动哪些文件
uv run replace_links.py

# 确认无误后 --apply 才真正写盘
uv run replace_links.py --apply

# ③ 构建反向引用表（写到同一个 manifest.db）
uv run build_refs.py
```

每条命令都支持纯 CLI 参数覆盖，跳过 `.env`：

```bash
uv run upload_oss.py --src /path/to/imgs --bucket my-bucket \
    --access-key LTAI... --access-secret s7...
```

`--help` 看完整参数列表。

## 📊 Dashboard 使用

### 在线 demo

直接访问 GitHub Pages：https://dao1023.github.io/obsidian-imgbed/

### 本地查看自己的数据

两种方式：

```bash
# 方式 A：双击打开 dashboard.html（file:// 协议，拖入 db 文件）
# 方式 B：用 bun 跑本地服务器（可选，开发更友好）
bun run serve.ts
# 然后访问 http://localhost:8787/
```

拖入你的 `manifest.db`，浏览器本地解析（sql.js SQLite WASM），**数据库不会上传到任何服务器**。

Dashboard 提供 4 个视图：

| Tab | 用途 |
|-----|------|
| 📊 总览 | 图片总数 / 被引用 / 孤儿图 / md 数 + 引用量分布柱图 |
| 🔍 搜索 | 按文件名模糊搜索，查看每张图的引用情况 |
| 🏆 Top 榜 | 引用量 Top 15 + 大小 Top 15 |
| 🗑️ 孤儿图 | 未被任何 md 引用的图，支持导出 CSV |

点列表里任意一行的「详情」可看该图被哪些 md 引用、缩略图（默认关闭以节省流量，按钮开启）、OSS Key 等。

## 🛠️ 工作原理

```
┌────────────┐    upload     ┌─────────────┐
│ Obsidian   │ ───────────▶  │ Aliyun OSS  │
│ images/    │               │   bucket    │
└────────────┘               └─────────────┘
      │                             │
      │  scan + sha256              │  oss_key
      ▼                             ▼
┌────────────────────────────────────────┐
│         manifest.db (SQLite)           │
│  ┌─────────┐  ┌──────┐  ┌──────────┐  │
│  │  files  │  │ refs │  │ref_summary│ │
│  └─────────┘  └──────┘  └──────────┘  │
└────────────────────────────────────────┘
      ▲                             ▲
      │ filename PK → O(1) lookup   │ scan wikilinks + imgbed URLs
      │                             │
┌────────────┐               ┌──────────────┐
│ replace_   │ ◀──────────── │ Markdown/    │
│ links.py   │ ────────────▶ │  *.md        │
└────────────┘               └──────────────┘
      │
      ▼
┌────────────┐
│ build_     │  扫描所有 md → 反向引用
│ refs.py    │  表 refs + 视图 ref_summary
└────────────┘
```

详细开发流水见 [docs/project_log.md](docs/project_log.md)。

## ⚠️ 已知限制

- **仅支持阿里云 OSS**（依赖 `oss2` SDK）。欢迎 PR 加 S3 / R2 / COS / 七牛 等后端。
- **OSS bucket 需要公共读权限**才能让 markdown 直接渲染图床 URL；私有 bucket 需改用签名 URL（未实现）。
- **`replace_links.py` 是一次性操作**，没有增量替换。运行前请备份或用 git 管理 markdown。
- **dashboard 缩略图默认关闭**，因为全量加载 1.2 万张图会消耗大量 OSS 流量；点击「打开预览」按钮主动启用。

## 🤝 贡献

欢迎 issue / PR。一些待办方向：

- [ ] 支持 AWS S3 / Cloudflare R2 / 腾讯云 COS
- [ ] 私有 bucket 的签名 URL 模式
- [ ] Dashboard 多 db 对比
- [ ] 增量替换（只处理新增的 wikilink）

## 📄 License

[MIT](LICENSE) · Copyright © 2026 Dao1023
