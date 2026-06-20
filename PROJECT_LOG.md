# OSS 图床迁移项目日志

> 目标：把 Obsidian 笔记中 1.2 万+ 本地图片迁移到阿里云 OSS，并把 Markdown 中的引用改为图床直链，同时建立图片↔笔记的反向引用关系。

---

## 时间线

### 1. 需求与信息收集
- 用户提出：`C:\Obsidian\assets\Images\Defalut` 下约 1.2 万张图片需全部上传对象存储，要求**不遗漏、不重复、失败自动重试**。
- 读取密钥文件 `oss.md`，确认是**阿里云 OSS**。
- 从 PicGo 配置 `C:\Users\Dao\AppData\Roaming\picgo\data.json` 反查出：
  - Bucket：`img-dao`
  - Region：`oss-cn-shanghai`
- 源目录实际清点：**12,832** 个文件（含 0 字节壳文件）。

### 2. 方案设计
通过 brainstorming 确定关键决策：
- **OSS key**：原始文件名（保留中文 / `$` / `{}` / 空格 / 括号 等特殊字符）— OSS key 原生支持任意 UTF-8，SDK 内部自动 URL 编码。
- **去重 / 断点续传**：SQLite 清单 `oss_upload_manifest.db`，schema 含 `filename`(PK), `size`, `mtime`, `sha256`, `oss_key`, `status`, `attempts`, `last_error`, `uploaded_at`。
- **失败重试**：单文件最多 5 次，指数退避 (1/2/4/8/16s)。
- **大文件断点续传**：≥50MB 走 `oss2.resumable_upload`。

### 3. 工程化（uv 管理）
- 用户偏好 `uv`，在 `C:\Users\Dao\ClaudeCode\1.oss` 用 `uv init --bare` 初始化。
- `uv add oss2 tqdm`：首次默认源超时，切换清华镜像成功。
- 依赖固定：oss2 2.19.1、tqdm 4.68.3。

### 4. 上传脚本 `upload_oss.py`
关键能力：
- 扫描源目录 → 入库（按 size/mtime 判断是否需重传）
- 8 线程并发上传
- 0 字节文件主动跳过
- `--scan-only` / `--retry-failed` / `--rescan` 子命令

### 5. 接口验证 → 发现账号问题
- 第一次单文件验证上传 `赞.jpg` → **403 `UserDisable`**（EC `0003-00000801`）。
- 诊断：`get_bucket_info` 能通，`put_object` / `list_objects` 被拦 → 典型**阿里云账号欠费**症状。
- 用户充值后重新验证 → 上传成功，size 校验一致，list 正常。

### 6. 全量上传
- 运行 `upload_oss.py` 完成。
- 结果：**done 12819**，failed 4（全是 0 字节壳文件，良性跳过），pending 0。
- 总大小约 **10.58 GB**，最大单图 `拼图2.JPG` 76.29 MB。

### 7. Markdown 引用替换需求
- 用户：13k+ md 文件，12k+ 图片，若 N×M 匹配会很慢 → 利用 DB 的 filename PK 做 O(1) 查表，降到 O(N)。
- 实测引用形态：`[[xxx.png]]` 8450 处 + `![](local/path)` 27 处，含 `|width` / `|caption` 后缀。

### 8. 替换脚本 `replace_links.py`
规则：
- `[[xxx.png]]` / `![[xxx.png]]` / `[[xxx.png|opts]]` → `![](<https://img-dao.oss-cn-shanghai.aliyuncs.com/<encoded>>)`
- URL 用 `<>` 包裹，避免 `)`/空格 破坏 markdown 解析
- 非图片扩展名 / DB 未命中 → 原样保留
- dry-run + `--apply` 双模式

### 9. dry-run 暴露计数 bug → 修复
- 首次 dry-run 显示"全部 13368 文件都将修改"，明显异常。
- 调试发现 `will_change_files` 计数器缩进错误（在 `if new != orig` 块外），每文件都计数。
- 修复后真实数字：**3469 文件被修改**，8450 wikilink 替换 + 1 markdown 替换。

### 10. miss 列表诊断 → 确认良性
- 158 个未命中引用 → 抽样检查发现这些图**根本不在上传源目录**（在 Obsidian 其他附件位置或已删除）。
- 脚本原样保留是对的，Obsidian 本地仍能解析。

### 11. apply 执行
- `replace_links.py --apply`：3469 文件修改，幂等性复跑确认 0 残留。

### 12. 4 个 failed 清理
- 用户问 failed 4 个是什么 → 全是 0 字节 Obsidian 粘贴残留文件，本地也已不存在。
- 从清单 `DELETE` 清理这 4 行。

### 13. 反向引用表 `build_refs.py`
- 用户的遗憾点：`replace_links.py` 没记录"哪个图被哪个 md 引用"。
- 解决方案：重新单次 O(N) 扫描所有 md（同时识别**残留 wikilink** 和**已替换的图床 URL**），构建反向关系。
- 输出：
  - `refs` 表 `(image_filename, md_path)`
  - `ref_summary` 视图（含 `ref_count`, `referenced_by`）
  - `image_refs.csv`（UTF-8 BOM，Excel 友好）
- 统计结果：
  - 被引用图片：**7930** (61.9%)
  - 孤儿图：**4889** (38.1%)
  - 引用过图片的 md：3463 / 13329
  - Top：`Pasted image 20260116001643.png` 被引 36 次

---

## 当前产物

```
C:\Users\Dao\ClaudeCode\1.oss\
├── pyproject.toml              # uv 项目
├── oss.md                      # 密钥
├── upload_oss.py               # 上传脚本
├── replace_links.py            # md 链接替换
├── build_refs.py               # 反向引用构建
├── oss_upload_manifest.db      # 主清单（含 files + refs + ref_summary 视图）
├── image_refs.csv              # 反向引用 CSV
├── oss_replace_miss.txt        # 158 个未命中引用
├── oss_upload_failed.txt       # （已清理，无内容）
├── .oss_store/                 # resumable_upload 分片临时目录
└── PROJECT_LOG.md              # 本文件
```

## DB Schema 速览

```sql
-- files：图片上传清单
filename      TEXT PRIMARY KEY
local_path    TEXT
size          INTEGER       -- bytes
mtime         REAL
sha256        TEXT
oss_key       TEXT
status        TEXT          -- pending/done/failed
attempts      INTEGER
last_error    TEXT
uploaded_at   REAL

-- refs：图片↔笔记 反向引用关系
image_filename TEXT
md_path        TEXT
PRIMARY KEY (image_filename, md_path)

-- ref_summary：视图
filename, size, ref_count, referenced_by (| 分隔)
```

## 经验教训

1. **替换/扫描类脚本要顺带记录明细**，不只统计总数 — 否则后续要重建关系需重新扫一遍。
2. **dry-run 计数器位置要小心**，缩进错误会让每个文件都被计入。
3. **0 字节壳文件**是 Obsidian 粘贴失败留下的，应该跳过而非当错误处理。
4. **`UserDisable` 403** ≠ 鉴权失败，要看 `get_bucket_info` 是否能通，能通就是欠费类账号级禁用。
5. **markdown `[](<url>)` 的尖括号**是处理含 `)`/空格 URL 的必备写法，不是装饰。
