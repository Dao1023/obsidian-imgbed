# obsidian-imgbed 开源设计

**日期**: 2026-06-20
**作者**: Dao + Claude
**状态**: 待批准

## 1. 目标

把现有「个人 OSS 图床迁移 + 反向引用可视化」项目改造为可复用的开源 CLI 工具仓库，命名 `obsidian-imgbed`，让其他人改 bucket/路径/AK 就能跑。

## 2. 定位

- **形态**：CLI 工具（argparse + `.env` 配置）
- **目标用户**：Obsidian 用户，主要在中国（Aliyun OSS 区域）
- **保留风格**：单文件脚本，不抽公共模块（用户偏好）
- **核心卖点**：上传 + 替换 + 反向引用 + 可视化 dashboard 的端到端方案

## 3. 仓库结构

```
obsidian-imgbed/
├── README.md               # 项目介绍 + 快速上手 + 截图
├── LICENSE                 # MIT
├── .gitignore              # .env / *.db / *.csv / oss.md
├── pyproject.toml          # uv 项目（oss2、tqdm）
├── .env.example            # 配置模板
│
├── upload_oss.py           # CLI: 上传图片
├── replace_links.py        # CLI: 替换 wikilink → 图床 URL
├── build_refs.py           # CLI: 反向引用表
│
├── dashboard.html          # 单文件可视化（原样保留）
├── serve.ts                # 本地调试（可选）
│
├── docs/
│   └── project_log.md      # 脱敏后的开发流水
│
├── examples/
│   └── sample_manifest.db  # 小样本 db（20 条假数据），用于 Pages demo
│
└── .github/workflows/
    └── deploy-pages.yml    # 自动部署 dashboard 到 Pages
```

## 4. CLI 重构

每个脚本改为 argparse，参数从 CLI 或 `.env` 读取（用 `os.environ.get`，零新依赖）。

### 4.1 `upload_oss.py`

```bash
uv run upload_oss.py \
  --src $OBSIDIAN_IMG_DIR \
  --bucket $OSS_BUCKET \
  --endpoint $OSS_ENDPOINT \
  --access-key $OSS_AK \
  --access-secret $OSS_SK \
  --db manifest.db \
  --threads 8
```

特性：SQLite 清单、断点续传、失败重试（指数退避）、≥50MB 分片上传。

### 4.2 `replace_links.py`

```bash
# 先 dry-run
uv run replace_links.py --md-dir $OBSIDIAN_MD_DIR --db manifest.db --url-prefix $OSS_URL_PREFIX --dry-run

# 应用
uv run replace_links.py ... --apply
```

特性：DB 哈希表 O(1) 查找、`[](<url>)` 包裹特殊字符、统计报告。

### 4.3 `build_refs.py`

```bash
uv run build_refs.py --md-dir $OBSIDIAN_MD_DIR --db manifest.db --csv-out refs.csv
```

特性：扫描 wikilink + 已替换的图床 URL，建 `refs` 表 + `ref_summary` 视图。

### 4.4 配置约定

`.env.example`：

```env
# Obsidian 路径
OBSIDIAN_IMG_DIR=/path/to/obsidian/assets/images
OBSIDIAN_MD_DIR=/path/to/obsidian/markdown

# OSS 凭证
OSS_BUCKET=your-bucket
OSS_ENDPOINT=oss-cn-shanghai.aliyuncs.com
OSS_AK=your-access-key
OSS_SK=your-access-secret
OSS_URL_PREFIX=https://your-bucket.oss-cn-shanghai.aliyuncs.com/

# DB
OSS_MANIFEST_DB=manifest.db
```

脚本顶部统一加：

```python
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # 不强制依赖
```

（可选 `python-dotenv`，加到 pyproject.toml；不装也能用纯环境变量跑）

## 5. 开源卫生清单

### 5.1 `.gitignore`

```
# 数据库与导出
*.db
*.db-wal
*.db-shm
*.csv

# 凭证
.env
oss.md

# Python
__pycache__/
.venv/
*.pyc

# 系统
.DS_Store
Thumbs.db
```

### 5.2 必须脱敏的文件

| 文件 | 当前状态 | 处理 |
|------|---------|------|
| `upload_oss.py` | 含 `C:\Obsidian\...`、`img-dao` | 改为 argparse 默认空 |
| `replace_links.py` | 含 `C:\Obsidian\...`、URL prefix | 改为 argparse |
| `build_refs.py` | 含 `C:\Obsidian\...`、`img-dao`、URL prefix | 改为 argparse |
| `serve.ts` | 含 `dashboard.html` 字面量 | 保留 |
| `test_queries.mjs` | 含 db 字面量 | 改为 argparse 或删除 |
| `oss.md` | **AccessKey 明文** | **不提交**，写进 .gitignore |
| `oss_upload_manifest.db` | 真实 12819 条文件名 | 不提交（已被 *.db） |
| `oss_upload_manifest_snapshot.db` | 同上 | 不提交 |
| `image_refs.csv` | 真实笔记路径 | 不提交（已被 *.csv） |
| `PROJECT_LOG.md` | 含真实路径、AK 提示 | 脱敏后改名 `docs/project_log.md` |

### 5.3 sample_manifest.db

写 `scripts/make_demo_db.py` 生成 20 条假图元数据：

```python
# filename: sample_001.png ... sample_020.png
# oss_key: samples/sample_001.png ...
# size: 随机 100KB-5MB
# refs: 给 5 张图随机分配 md_path（fake_note_01.md 等）
```

输出 `examples/sample_manifest.db`，进 git。

### 5.4 LICENSE

MIT，版权 `Copyright (c) 2026 Dao`。

## 6. README 大纲

1. **项目横幅**：一句话定位 + 截图（dashboard 全景）
2. **✨ 特性**：上传/替换/反向引用/可视化 4 个 bullet
3. **📸 截图**：dashboard 4 个 tab 各一张
4. **🚀 快速上手**：
   - clone + `uv sync`
   - 配置 `.env`
   - 三条命令完成迁移
5. **📊 Dashboard 使用**：拖入 `.db` 即用 + 在线 demo 链接
6. **🛠️ 工作原理**：流程图（图片→OSS→URL→替换 md→反向引用）
7. **⚠️ 已知限制**：仅 Aliyun OSS（欢迎 PR 加 S3/R2/COS）、需要 bucket 公共读或签名 URL
8. **🤝 贡献**：欢迎 issue/PR
9. **📄 License**：MIT

## 7. GitHub Pages + Actions

### 7.1 dashboard 行为

- 自动加载 `/sample_manifest.db`（用户选择）
- 加载失败回退到拖拽区
- 顶部加 banner：「演示数据，点此加载自己的 db」

### 7.2 `deploy-pages.yml`

```yaml
name: Deploy Pages
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
  pages: write
  id-token: write
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: github-pages
    steps:
      - uses: actions/checkout@v4
      - name: Build
        run: |
          mkdir -p site
          cp dashboard.html site/index.html
          cp examples/sample_manifest.db site/
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with: { path: site }
      - uses: actions/deploy-pages@v4
```

## 8. 实施顺序

1. 脱敏 + 重构 3 个 Python 脚本（argparse + env）
2. 写 `.gitignore`、`.env.example`、LICENSE
3. 写 `scripts/make_demo_db.py` 生成 sample db
4. 改 dashboard.html（自动加载 sample + banner）
5. 脱敏 PROJECT_LOG → `docs/project_log.md`
6. 写 README
7. 写 `.github/workflows/deploy-pages.yml`
8. `git init` + 首次 commit
9. GitHub 建仓库 + push
10. 启用 Pages，触发 Actions

## 9. 风险与未决

- **真实 db 在 git history 中**：本次首次 commit 前确认 db 没进，无历史泄漏
- **dashboard.html 含 OSS prefix 字面量**：保留（用户改 bucket 时也得改 prefix，README 说明）
- **未支持 S3/R2**：README 明确，加 issue 模板欢迎 PR
- **replace_links.py 是一次性操作**：不提供增量替换（README 说明，避免误用）
