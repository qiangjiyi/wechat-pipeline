<div align="center">

# WeChat Pipeline

**面向 Claude Code 与 Codex 的微信公众号多 Agent 发布流水线**

从原始内容出发，依次完成格式化、图片规划与生成、gzh-design 公众号排版、产物验收，并安全写入微信公众号草稿箱。

[![Claude Code Plugin](https://img.shields.io/badge/Claude_Code-Plugin-D97757?style=flat-square)](https://docs.anthropic.com/en/docs/claude-code)
[![Codex Plugin](https://img.shields.io/badge/Codex-Plugin-111827?style=flat-square)](https://developers.openai.com/codex/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-07C160?style=flat-square)](LICENSE)

[快速开始](#快速开始) · [工作原理](#工作原理) · [配置](#配置) · [使用示例](#使用示例) · [故障排查](#故障排查)

</div>

> [!IMPORTANT]
> 本项目只创建微信公众号**草稿**，不会自动群发。第一次使用建议先在测试账号或非生产账号完成验证。

## 为什么需要它

一篇微信内容从原稿到草稿箱，通常涉及多个彼此依赖的步骤：整理 Markdown、规划视觉、生成图片、适配微信格式、上传素材并创建草稿。让单个 Agent 临场完成全部工作，容易出现重复运行、绕过 Skill、伪造中间文件、修改提示词或在错误目录重新生成等问题。

WeChat Pipeline 将这些步骤拆成职责明确的 Agent，并通过统一运行协议约束执行过程：

- **一次请求，一个运行目录**：格式化、prompt、图片、manifest 和发布适配文件都属于同一个 `run_id`。
- **Native Skill First**：Designer 与 Typesetter 必须读取并执行完整 Baoyu / gzh-design Skill，不以协调器摘要代替真实 Skill 流程。
- **先规划、后生成、再发布**：图片生成前执行 plan 校验，发布前执行 publish-ready 校验。
- **自然产物优先**：保留 Skill 原本的文件名和目录结构，不为了验收强制重命名或补造文件。
- **失败原地恢复**：网络重试、provider fallback 和恢复执行都复用同一个运行，不创建第二套产物。
- **双宿主分发**：同一个 Plugin 同时支持 Claude Code 与 Codex，无需修改全局 `CLAUDE.md` 或 `AGENTS.md`。

## 功能概览

| 能力 | 微信贴图 `newspic` | 微信文章 `news` |
|---|---:|---:|
| 原始内容接入 | Markdown / YAML / JSON / 对话正文 | Markdown / 对话正文 |
| 内容格式化 | 按需跳过 | `baoyu-format-markdown` |
| 图片生成 | `baoyu-xhs-images` | 封面 + 文章内联图 |
| 公众号排版 | 不适用 | `gzh-design` 主题组件库 + HTML 校验 |
| 图片 Provider fallback | 支持 | 支持 |
| 两阶段产物验收 | 支持 | 支持 |
| 多公众号账号 | 支持 | 支持 |
| 微信草稿箱发布 | 1-20 张图片 | 带主题 HTML 文章 |
| 自动正式群发 | 不支持 | 不支持 |

内置的完整 Skill：

- `wechat-pipeline`：跨宿主总入口与 Codex 协调器
- `wechat-pipeline-setup`：初始化配置与环境诊断
- `wechat-publisher`：微信公众号草稿发布
- `baoyu-format-markdown`
- `baoyu-xhs-images`
- `baoyu-cover-image`
- `baoyu-article-illustrator`
- `baoyu-image-gen`
- `gzh-design`

安装 Plugin 后不需要再把这些 Skill 单独复制或软链接到 `~/.claude/skills/`、`~/.codex/skills/`。

## 前置条件

| 依赖 | 要求 | 用途 |
|---|---|---|
| Claude Code 或 Codex | 支持 Plugin 的当前版本 | 加载入口、Agent 与 Skill |
| Python | 3.10+ | 运行上下文、校验器和 Publisher |
| Node.js + npm | 仅旧 Markdown Publisher 路径需要 | 兼容旧 `baoyu-md` 渲染器 |
| 微信公众号凭据 | App ID + App Secret，或 Access Token | 创建草稿与上传素材 |
| 图片生成能力 | Codex/宿主原生能力或 Baoyu 支持的 Provider | 生成卡片、封面和插图 |

当微信 API 需要固定出口 IP 时，可配置自己的 HTTP Worker 代理；项目不会附带或托管代理服务。

## 快速开始

### 1. 安装 Plugin

#### Claude Code

从 GitHub marketplace 安装：

```bash
claude plugin marketplace add qiangjiyi/wechat-pipeline
claude plugin install wechat-pipeline@jiyi-plugins
```

也可以在 Claude Code 会话内使用 `/plugin marketplace add` 和 `/plugin install`。

#### Codex

```bash
codex plugin marketplace add qiangjiyi/wechat-pipeline
codex plugin add wechat-pipeline@jiyi-plugins
```

安装后请新建一个 Claude Code 或 Codex 会话，让宿主重新加载 Plugin 注册表。

### 2. 初始化配置

Claude Code：

```text
/wechat-pipeline:wechat-pipeline-setup
```

Codex：

```text
$wechat-pipeline:wechat-pipeline-setup
```

Setup 会创建权限为 `0600` 的配置文件：

```text
~/.config/wechat-pipeline/.env
```

填写凭据后执行诊断：

```text
# Claude Code
/wechat-pipeline:wechat-pipeline-setup --mode newspic --account personal

# Codex
$wechat-pipeline:wechat-pipeline-setup --mode newspic --account personal
```

### 3. 发起一次流水线任务

Claude Code 推荐使用明确的 Leader Agent：

```text
@wechat-pipeline:wechat-leader 把 /absolute/path/draft.md 制作成微信贴图，发布到 personal 公众号草稿箱
```

Codex 使用主编排 Skill：

```text
$wechat-pipeline:wechat-pipeline 把 /absolute/path/draft.md 制作成微信贴图，发布到 personal 公众号草稿箱
```

任务成功后会返回公众号账号、模式、标题、图片数量、草稿 `media_id` 和唯一产物目录。

## 本地安装测试

首次 push 前后都可以从本地目录模拟安装，不需要依赖全局 Agent 或 Skill 软链接。

```bash
git clone https://github.com/qiangjiyi/wechat-pipeline.git
cd wechat-pipeline
```

Claude Code：

```bash
claude plugin marketplace add /absolute/path/to/wechat-pipeline
claude plugin install wechat-pipeline@jiyi-plugins
```

Codex：

```bash
codex plugin marketplace add /absolute/path/to/wechat-pipeline
codex plugin add wechat-pipeline@jiyi-plugins
```

更新本地代码后，如果宿主仍在使用安装缓存，可卸载再安装：

```bash
# Claude Code
claude plugin uninstall wechat-pipeline@jiyi-plugins
claude plugin install wechat-pipeline@jiyi-plugins

# Codex
codex plugin remove wechat-pipeline@jiyi-plugins
codex plugin add wechat-pipeline@jiyi-plugins
```

> [!TIP]
> 做自包含性测试时，可以暂时停用全局同名 Baoyu Skills。流水线必须仍能从 Plugin 命名空间调用内置 Skill。

## 配置

### 多账号配置

```env
WECHAT_PROXY_URL=https://your-proxy.example.com/
WECHAT_API_BASE=https://api.weixin.qq.com

WECHAT_ACCOUNTS=personal,company

WECHAT_PERSONAL_APP_ID=wx...
WECHAT_PERSONAL_APP_SECRET=...
WECHAT_PERSONAL_ACCESS_TOKEN=

WECHAT_COMPANY_APP_ID=wx...
WECHAT_COMPANY_APP_SECRET=...
WECHAT_COMPANY_ACCESS_TOKEN=
```

账号别名会转换为大写环境变量名，非字母数字字符转换为下划线。例如 `brand-cn` 对应 `WECHAT_BRAND_CN_APP_ID`。

### 单账号配置

当 `WECHAT_ACCOUNTS` 为空时，可以使用：

```env
WECHAT_APP_ID=wx...
WECHAT_APP_SECRET=...
WECHAT_ACCESS_TOKEN=
```

### 配置优先级

Publisher 按以下优先级读取配置，前者覆盖后者：

1. 当前进程环境变量
2. 显式 `--env-file`
3. `WECHAT_PUBLISHER_ENV_FILE`
4. 内容文件旁的 `.env.local` / `.env`
5. `~/.config/wechat-pipeline/.env.local` / `.env`

命名账号不会回退到全局 `WECHAT_APP_ID`，这是为了避免多账号场景把内容发到错误公众号。

### 图片 Provider

流水线优先使用宿主可用的原生图片能力。其他 Provider 按 `baoyu-image-gen` 的约定配置在：

```text
~/.baoyu-skills/.env
```

预检只报告 Provider 是否可用，不会输出 API Key。真实渲染过程中，provider 或模型不兼容会被记录为 `api_error`，后续 fallback 必须复用同一份 prompt hash。

## 使用示例

### 微信贴图

```text
把下面这篇内容做成 6 张微信贴图，使用 personal 账号，只保存到草稿箱：

<正文内容>
```

也可以传入本地 Markdown：

```text
把 /absolute/path/topic.md 制作成微信贴图并发布到 personal 草稿箱
```

### 微信文章

```text
把 /absolute/path/article.md 排版成微信公众号文章，生成封面和必要插图，发布到 company 草稿箱
```

如果输入已经具有可用的 Markdown 标题或 frontmatter，Formatter 会明确标记为 skipped，不生成占位文件。流水线随后创建一次 `article-source.md` 工作副本，让 Baoyu article-illustrator 原生插入图片引用；不可变原稿始终保留在 `.pipeline/input.md`。Typesetter 可在 Designer 生成过程中提前启动，与剩余图片生成并行；未完成的图片先留占位符，全部生成后补全 HTML。图片全部完成后，publish-ready 校验与 `gzh-design` 排版并发执行：用户指定主题时按指定主题，未指定时按文章题材自动选择；最终只把通过双重 HTML 门禁的正文片段交给 Publisher。

### 只使用 Publisher

已有最终图片、Markdown 或经过验收的 gzh-design HTML 时，可单独调用发布 Skill：

```text
# Claude Code
/wechat-pipeline:wechat-publisher

# Codex
$wechat-pipeline:wechat-publisher
```

Publisher 支持：

- `newspic`：短文本 + 1-20 张本地图片
- `article --html`：发布经过 layout manifest 验收的 gzh-design HTML，上传正文图和永久封面素材
- `article <markdown>`：显式使用旧 Markdown renderer 的兼容路径
- `--dry-run`：校验并展示最终计划，不发起微信写入
- `--result-output <path> --verify-draft`：原子保存草稿回执并通过 `draft/get` 回读验收；完整流水线固定启用
- newspic 完整流水线固定使用 `--manifest` 绑定 sealed 原文、图片顺序与 hash；素材上传结果按张持久化恢复

## 工作原理

```mermaid
flowchart LR
    U["用户请求"] --> L["Leader / Coordinator"]
    L --> R["创建或复用唯一 Run"]
    R --> D["Doctor 环境预检"]
    D --> F["Formatter（按需）"]
    F --> P1["Designer 规划 prompt"]
    P1 --> V1["Plan 校验"]
    V1 --> G["执行 Baoyu Skill 生图"]
    G --> J1{"图片全部完成"}
    J1 --> V2["Publish-ready 校验"]
    J1 --> T["Typesetter 执行 gzh-design"]
    T --> LV["Layout HTML 校验"]
    V2 --> J2{"双门禁通过"}
    LV --> J2
    J2 --> P2["Publisher 上传图片并替换 src"]
    P2 --> W["创建唯一草稿并原子写回执"]
    W --> RV["draft/get 回读验收"]
    RV --> DONE["published"]
```

Claude Code 由 `wechat-leader` Agent 独占协调；Codex 由 `wechat-pipeline` Skill 作为逻辑 Leader 调度子 Agent。两者共享同一份运行协议和校验脚本。

状态只能按实际阶段流转：

```text
newspic: input_sealed -> planning -> rendering -> ready -> publishing -> published
news:    input_sealed -> planning -> rendering -> ready -> typesetting -> layout_ready -> publishing -> published
```

失败状态记录失败前阶段，恢复时回到同一个 `run_id`；`published` 和 `cancelled` 是终态。
状态只由 Leader 推进，并自动追加到 `.pipeline/events.jsonl`。草稿已经创建但回读中断时，恢复流程只重试验证，不再调用 `draft/add`。

### 运行目录

根目录可在进程环境或 `~/.config/wechat-pipeline/.env[.local]` 中配置：

```text
${WECHAT_PIPELINE_EXPORTS_DIR:-$HOME/Workspace/exports}
```

典型结构：

```text
exports/
├── image-cards/
│   └── <slug>-<run_id>/
│       ├── .pipeline/
│       │   ├── input.md
│       │   ├── run.json
│       │   ├── events.jsonl
│       │   ├── progress.json
│       │   ├── doctor.json
│       │   ├── manifest.json
│       │   ├── publish-result.json
│       │   └── publish-result.lock
│       ├── prompts/
│       └── *.png
└── wechat-articles/
    └── <slug>-<run_id>/
        ├── article-source.md
        ├── article-body.html
        └── .pipeline/
            ├── layout.json
            └── layout-validation.json
```

`.pipeline/` 保存审计与验收元数据；用户可见图片、prompt 和 Skill 自然产物保留在同一 canonical 目录。

完整契约见 [wechat-pipeline-protocol.md](plugins/wechat-pipeline/docs/wechat-pipeline-protocol.md)。

## 项目结构

```text
wechat-pipeline/
├── .claude-plugin/                 # Claude marketplace
├── .agents/plugins/                # Codex marketplace
├── plugins/wechat-pipeline/
│   ├── .claude-plugin/             # Claude Plugin manifest
│   ├── .codex-plugin/              # Codex Plugin manifest
│   ├── agents/                     # 5 个 Claude Code Agents
│   ├── skills/                     # 协调、发布、Baoyu 与 gzh-design Skills
│   ├── scripts/                    # Run、Doctor 和 Manifest 校验器
│   ├── shared/                     # 跨脚本共享实现
│   ├── docs/                       # 运行协议
│   └── third_party/                # 上游快照锁与 License
├── docs/                            # 维护决策
├── scripts/                         # 上游 Skill 快照维护工具
└── tests/                           # 结构、协议与 Publisher 测试
```

## 内置 Baoyu Skills

Baoyu Skill 源码来自 [JimLiu/baoyu-skills](https://github.com/JimLiu/baoyu-skills)，固定到 commit `6b7a2e417500561a5ecdd0b168332f4142584617`。

固定完整快照有两个目的：

1. Claude Code 与 Codex 一次安装即可运行，不依赖用户机器上是否存在其他版本。
2. Skill 内容、reference 和脚本可通过 tree SHA-256 验证，避免上游更新静默改变流水线行为。

### 同步上游更新

先使用本机的 Skill 更新工具拉取 Baoyu 源仓库，再运行本项目的同步器：

```bash
python3 scripts/sync_baoyu_skills.py --check
python3 scripts/sync_baoyu_skills.py
```

默认源仓库路径：

```text
~/Workspace/downloads/skill-sources/baoyu-skills
```

其他维护者可以通过参数或环境变量指定已经更新且工作区干净的本地 clone：

```bash
python3 scripts/sync_baoyu_skills.py --source /path/to/baoyu-skills

BAOYU_SKILLS_SOURCE=/path/to/baoyu-skills \
  python3 scripts/sync_baoyu_skills.py
```

同步器不联网，也不执行 `git pull`。它会读取本地源仓库的 HEAD，逐个比较五个 Skill 的完整目录 hash；发生变化的目录会先复制到 staging 并校验，再整体替换，因此能同时处理上游新增、修改和删除的文件。

同步成功后会自动：

- 更新 `plugins/wechat-pipeline/third_party/baoyu-skills.lock.json` 中的 commit、Skill version 和 tree SHA-256
- 更新 `plugins/wechat-pipeline/THIRD_PARTY_NOTICES.md` 中的上游 commit
- 同步上游 License
- 同时提高 Claude/Codex Plugin patch 版本
- 在 `CHANGELOG.md` 记录同步 commit

常用保护选项：

```bash
# 只显示预计变化
python3 scripts/sync_baoyu_skills.py --dry-run

# vendored 目录存在未提交修改时默认拒绝覆盖；确认丢弃后才使用
python3 scripts/sync_baoyu_skills.py --force
```

升级快照后必须确认：

1. `git diff` 只包含预期的上游变化和元数据更新。
2. Claude/Codex manifest 版本保持一致。
3. 全部测试与两个宿主的 manifest 验证通过。

不要只替换单个 `SKILL.md`。

## 内置 gzh-design Skill

公众号文章排版使用 [isjiamu/gzh-design-skill](https://github.com/isjiamu/gzh-design-skill) 的原样运行快照，固定提交和完整 runtime tree SHA-256 记录在：

```text
plugins/wechat-pipeline/third_party/gzh-design.lock.json
```

流水线不会修改它的 `SKILL.md`、主题库、通用组件或校验脚本；自定义约束全部放在 `wechat-typesetter` worker 和 layout manifest 门禁中。

从已更新的本地上游仓库同步：

```bash
python3 scripts/sync_gzh_design_skill.py \
  --source /path/to/gzh-design-skill \
  --check

python3 scripts/sync_gzh_design_skill.py \
  --source /path/to/gzh-design-skill
```

同步器只复制运行必需的 `SKILL.md`、`references/`、`scripts/`、`assets/`，并校验整树 hash；不会联网或执行 `git pull`。

## 安全边界

- 真实 `.env`、API Key、Access Token 和私钥不进入仓库。
- Setup 不覆盖已有配置，创建的新配置权限为 `0600`。
- Doctor 只检查配置是否存在，不打印密钥，也不在线验证 token。
- 默认只创建草稿，不执行正式群发。
- 命名账号凭据严格隔离，不回退到全局账号。
- Publisher 仅对可安全重试的读取和素材上传执行 30/60/120 秒退避；非幂等 `draft/add` 永不自动重试，结果不确定时阻止重复创建。
- 所有运行脚本通过 `run_python.sh` 自动选择 Python 3.10+；也可在进程环境显式设置 `WECHAT_PIPELINE_PYTHON`。
- `published` 必须同时具备持久化 `draft_media_id` 和成功的草稿回读验证。
- 长耗时 worker 通过 `progress.json` 报告结构化进度；图片尺寸、比例和重复输出在 publish-ready 阶段阻断。
- Agent 不得绕过 manifest 校验补文件、改 prompt、重命名图片或自行伪造 Skill 产物。

## 本地开发

### 验证 Plugin manifest

```bash
claude plugin validate --strict plugins/wechat-pipeline

python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py \
  plugins/wechat-pipeline
```

### 运行测试

```bash
python3 -m unittest discover -s tests -v
```

### 已排版 Article dry-run

```bash
python3 plugins/wechat-pipeline/skills/wechat-publisher/scripts/publish.py \
  article \
  --html /absolute/run/article-body.html \
  --layout-manifest /absolute/run/.pipeline/layout.json \
  --result-output /absolute/run/.pipeline/publish-result.json \
  --verify-draft \
  --account personal \
  --env-file plugins/wechat-pipeline/skills/wechat-publisher/.env.example \
  --dry-run
```

只有显式使用旧 Markdown renderer 时，Node 依赖才会通过 `npm ci` 安装到宿主数据或缓存目录；gzh-design HTML 发布路径不会创建 `node_modules`。

## 故障排查

<details>
<summary><strong>安装后找不到 Agent 或 Skill</strong></summary>

确认安装的是 `wechat-pipeline@jiyi-plugins`，然后新建会话。Plugin 内容通常会被宿主缓存，源码更新后需要卸载并重新安装。

</details>

<details>
<summary><strong>Doctor 提示账号未配置</strong></summary>

检查 `~/.config/wechat-pipeline/.env` 中的账号别名和对应字段。`personal` 必须使用 `WECHAT_PERSONAL_*`，不会回退到全局字段。

</details>

<details>
<summary><strong>同一篇内容为什么没有创建第二个目录</strong></summary>

这是预期行为。运行上下文按模式、账号和原文 SHA-256 复用未终结的运行，以避免重复生图和重复上传。需要独立运行时由内部命令显式使用 `--force-new`。

</details>

<details>
<summary><strong>正文第一张图作为封面时为什么会上传两次</strong></summary>

正文图片通过 `uploadimg` 得到可嵌入 HTML 的 mmbiz URL；文章封面要求永久素材的 `thumb_media_id`。两种微信 API 产物不能互相替代。

</details>

<details>
<summary><strong>为什么 YAML 不支持嵌套或多行值</strong></summary>

Publisher 的 YAML 输入有意限制为顶层标量和列表，避免引入隐式解析差异。复杂输入请使用 JSON 或 Markdown。

</details>

<details>
<summary><strong>为什么配置文件的值没有生效</strong></summary>

进程环境变量优先于配置文件。检查当前 shell 是否残留 `WECHAT_*` 或 `WECHAT_PUBLISHER_ENV_FILE`，但不要把真实值贴到 issue、日志或聊天中。

</details>

## 贡献

欢迎提交 Issue 和 Pull Request。修改运行协议、Agent 边界或内置 Skill 快照时，请同时更新测试和相关文档。

提交前运行：

```bash
python3 -m unittest discover -s tests -v
claude plugin validate --strict plugins/wechat-pipeline
```

建议使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式提交变更。

## 致谢

- [JimLiu/baoyu-skills](https://github.com/JimLiu/baoyu-skills)：提供格式化、图片规划和图片生成 Skills。
- [isjiamu/gzh-design-skill](https://github.com/isjiamu/gzh-design-skill)：提供公众号主题组件库、排版工作流与 HTML 合规校验。
- Claude Code 与 Codex Plugin 生态：提供 Agent、Skill 与本地 Plugin 运行能力。

第三方版权和固定版本信息见 [THIRD_PARTY_NOTICES.md](plugins/wechat-pipeline/THIRD_PARTY_NOTICES.md)。

## License

本项目基于 [MIT License](LICENSE) 开源。
