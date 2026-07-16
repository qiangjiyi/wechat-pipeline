---
protocol_version: 2026-07-13-001
protocol_status: active
authority: single-source-of-truth
---

# 微信发布流水线协议

本文档是 Claude Code 与 Codex 两种宿主共同使用的唯一运行协议。与 Agent/Skill 摘要、README 或调用方附加说明冲突时，以本文档为准。

本协议随 `wechat-pipeline` Plugin 安装：

- Claude Code 使用 `${PIPELINE_ROOT}/agents/` 中的五个原生 Agent；Codex 使用 `wechat-pipeline` 主 Skill 调度 formatter、designer、typesetter、publisher worker。
- 五个经过固定上游 commit 校验的 Baoyu Skill 内置于 `${PIPELINE_ROOT}/skills/`，两个宿主读取同一份原文。
- `gzh-design` 以固定上游 commit 的原样快照内置；排版 worker 必须直接读取其当前原文，不得由协调器复刻组件或工作流。
- 发布能力来自本 Plugin 内置的 `wechat-pipeline:wechat-publisher` Skill。
- Claude Code 从 `${CLAUDE_PLUGIN_ROOT}` 得到 `PIPELINE_ROOT`；Codex 从已加载的 `skills/wechat-pipeline/SKILL.md` 绝对路径向上两级得到 `PIPELINE_ROOT`。
- 所有项目内脚本通过已解析的 `PIPELINE_ROOT` 定位，不依赖 clone 路径或用户全局指令文件。

## 1. 所有权与路由

### 独占所有权

自然语言微信发布请求一旦交给 Claude 的 `wechat-leader` 或 Codex 的 `wechat-pipeline` 主 Skill，该逻辑 Leader 就拥有本次运行的独占所有权：

- 外层调用者只能原样传递用户请求、用户明确指定的账号/模式/视觉参数。
- 外层调用者不得补充自己推导的风格、配色、图片数量、字数限制或输出目录。
- Leader 返回失败后，只允许恢复同一个 Leader/`run_id`，或把失败透明报告给用户。
- 禁止外层调用者绕过 Leader，直接调用 Baoyu、publisher 或创建第二个输出目录。
- worker 不接受没有 `PIPELINE_ROOT`、`run_id`、`canonical_output_dir`、`protocol_version` 的派工。

这些规则全部随 Plugin 分发，不依赖全局 `CLAUDE.md` 或 `AGENTS.md`。

### 终态握手

Leader 每次回报末尾必须包含：

```text
WECHAT_PIPELINE_RESULT
protocol_version: 2026-07-13-001
run_id: <run_id>
canonical_output_dir: <absolute path>
status: published | failed | blocked
owner: wechat-leader
next_action: report_to_user | resume_same_leader
direct_skill_fallback_allowed: false
```

该握手用于告诉调用者：请求已经有唯一所有者，失败不是重新发起另一套 Skill 流程的授权。

## 2. 运行上下文与唯一目录

Leader 在派任何 worker 前，必须创建运行上下文：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/run_context.py" init \
  --mode <newspic|news> \
  --account <account> \
  --slug <ascii-slug> \
  [--source /absolute/path/to/source.md]
```

- `WECHAT_PIPELINE_EXPORTS_DIR` 可来自进程环境或 `~/.config/wechat-pipeline/.env[.local]`；未配置时使用 `$HOME/Workspace/exports`。
- `newspic` 落在 `<exports-root>/image-cards/<slug>-<run_id>/`，`news` 落在 `<exports-root>/wechat-articles/<slug>-<run_id>/`。
- 命令返回的目录是本次运行唯一的 `canonical_output_dir`。
- worker 只能在该目录内写文件，禁止创建平行目录。
- 重试、fallback、恢复执行全部复用同一个 `run_id`、目录和已落盘 prompt。

如果输入来自聊天而不是本地文件：

1. Leader 先创建权限为 `0600` 的临时文件，并通过宿主文件写入工具逐字写入用户原文；禁止用 shell 插值正文。
2. 把临时文件的绝对路径作为 `init --source` 输入，使脚本在创建目录前即可计算 hash 和查找 reusable run。
3. 用 `try/finally` 包裹初始化，无论 `init` 成功、失败或被中断都删除临时文件。正常流水线不得先创建 `awaiting_input` 运行再补正文；`seal` 子命令只保留给旧运行恢复。

`.pipeline/input.md` 是权限 `0400` 的不可变原始输入，运行目录及 `.pipeline/` 使用 `0700`。任何发布适配文件都不能冒充原始输入。

创建或复用已 seal 输入的运行后，Leader 必须运行：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/plugin_doctor.py" \
  --mode <newspic|news> --account <account> \
  --output <run-dir>/.pipeline/doctor.json
```

所有 Python 脚本必须通过 `run_python.sh` 启动。它优先使用 `WECHAT_PIPELINE_PYTHON`，否则自动查找 Python 3.10+；Doctor 未通过时停止派工并报告缺失配置。不得读取或输出真实密钥值。

Doctor 通过后，状态必须按实际阶段推进：

```text
newspic: input_sealed -> planning -> rendering -> ready -> publishing -> published
news:    input_sealed -> planning -> rendering -> ready -> typesetting -> layout_ready -> publishing -> published
```

- 所有状态转换只能由 Leader 使用 `run_context.py status ... --actor wechat-leader` 写入，worker 只回报产物与证据。
- Leader 在派第一个 worker 前设为 `planning`。
- Leader 在 Designer plan 校验通过后设为 `rendering`，publish-ready 校验通过后设为 `ready`。
- Leader 在 News Typesetter 开始前设为 `typesetting`，HTML 与 layout manifest 复核通过后设为 `layout_ready`。
- Leader 在 Publisher 派工前设为 `publishing`，持久化发布回执及草稿回读验证通过后设为 `published`。
- 任一活动阶段失败可设为 `failed`；恢复时回到失败前对应的活动阶段。
- `published` 和 `cancelled` 是终态，不得回退。

## 3. 模式与角色

- 用户明确说贴图、卡片、newspic、图片消息：`newspic`。
- 用户明确说长文、文章、news：`news`。
- 无显式信号时，超过 1200 字符或已是结构化 markdown：`news`；否则 `newspic`。

路径：

```text
newspic: Leader -> designer -> publisher
news:    Leader -> [formatter when input needs formatting] -> designer -> typesetter -> publisher
```

Leader 只负责识别、派工、验收、恢复和汇报，不亲自写 prompt、生图、装配 HTML 或发布。
当 sealed input 已经具有可用 Markdown 标题/frontmatter 时，Leader 记录 formatter skipped 并直接派 designer；不得为了保持固定链路而空跑 formatter。

Formatter 跳过时使用明确回报：`skipped: true`、`reason: already_structured_markdown`、`skill_files_read: []`、`natural_output_path: <run-dir>/.pipeline/input.md`。该路径表示复用 sealed input，不表示 Formatter 生成了新产物。

Formatter 完成或跳过后，Leader 必须调用：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/prepare_article_source.py" \
  <run-dir> --source <formatter-natural-output-path>
```

脚本只做字节复制，固定创建 `<run-dir>/article-source.md` 和 `.pipeline/article-source.json`。Designer 的 article-illustrator 以该可写副本为输入并原生插入图片引用；Typesetter 随后读取同一文件。重试时复用已存在的工作副本，不覆盖 Designer 已插入的引用。`.pipeline/input.md` 始终是不可变审计原文。

## 4. Native Skill First

Designer 必须执行目标 Baoyu Skill 的当前原文，而不是按 Agent 摘要模拟：

| 模式 | 必读 |
|---|---|
| newspic | `wechat-pipeline:baoyu-xhs-images` + 当前分支要求的 references + 首个命中的 `EXTEND.md` |
| news 封面 | `wechat-pipeline:baoyu-cover-image` + 当前分支要求的 references + `EXTEND.md` |
| news 内联图 | `wechat-pipeline:baoyu-article-illustrator` + 当前分支要求的 references + `EXTEND.md` |

流水线只追加用户明确参数、非交互信号和发布所需 aspect：

- `baoyu-xhs-images`：`--yes --aspect 3:4`
- `baoyu-cover-image`：`--quick --aspect 2.35:1`
- `baoyu-article-illustrator`：`直接生成 / 跳过确认`，aspect `16:9`

用户没有明确指定时，Leader 和外层调用者不得指定风格、调色、布局或图片数量。它们由 Skill 的原生分析与 `EXTEND.md` 决定。

Typesetter 必须完整执行内置 `wechat-pipeline:gzh-design`：读取 `SKILL.md`、`theme-index.md`、选中主题库与 `common-components.md`，按原组件装配并调用原校验器。流水线只追加以下调用约束，不修改上游文件：

- 用户明确主题优先；未指定时由 gzh-design 按题材自动选择并记录 `theme_source: auto`。
- 内容策略固定为 `preserve-visible-text`：不改写、不删减、不自动追加签名/CTA/观点；允许结构编号、目录与纯装饰标签。
- 本地图片 `src` 必须写成可解析的绝对路径并直接引用原生图片。
- 最终 HTML 不得包含占位符；上游 validator 的 ERROR 与 WARNING 都必须为零。
- 发布 run 不执行 gzh-design 的自定义主题生成工作流；该流程会修改主题注册表，必须作为独立维护任务处理，不能污染固定快照或当前 run。

## 5. 自然产物与隐藏审计

### 自然产物

Baoyu Skill 产生什么就保留什么，文件名与目录结构以该 Skill 当前版本为准。流水线不得为了固定清单而：

- 重命名或复制 prompt/图片；
- 补写假的 `analysis.md`、`outline.md`；
- 强制生成 Skill 本身不需要的 `batch.json`；
- 把失败预检包装成每张图的生成 attempt。

`batch.json` 仅在实际 backend 原生使用 batch 文件时保留，不是流水线必交付物。

### 隐藏审计

流水线自己的元数据统一放在 `<run-dir>/.pipeline/`：

```text
.pipeline/
├── run.json
├── events.jsonl             # 只追加的阶段、校验与恢复审计事件
├── input.md
├── preflight.json
├── manifest.json
├── article-source.json      # 仅 news，工作副本的来源证据
├── layout.json             # 仅 news，gzh-design 排版证据
├── layout-validation.json  # 仅 news，确定性校验结果
├── publish-result.json     # 草稿 media_id、请求指纹与回读验证
├── publish-result.lock     # 序列化并发发布/恢复，防止重复 draft/add
├── progress.json           # worker 原子更新的结构化阶段进度与心跳
└── publish-source.md       # 仅旧 Markdown publisher 路径需要时创建
```

News 另有 `<run-dir>/article-source.md` 与 `<run-dir>/article-body.html`。前者是 Formatter 结果的可写副本，允许 Baoyu article-illustrator 原生插图；后者是 Typesetter 生成的 gzh-design 正文片段。二者重试时都复用，除非协议明确允许创建新版本。

这些是运行证据，不是 Baoyu 或 gzh-design 原生产物。不得把它们伪装成 Skill 产物。

长耗时 worker 必须用 `run_context.py progress` 更新 `.pipeline/progress.json`。Designer 至少在规划完成、每张图完成和全部完成时更新；Publisher 至少在 token、逐张素材上传、草稿创建与回读完成时更新。progress 不改变 run 状态。

## 6. Provider 预检与 fallback

在生成 prompt 前运行：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/preflight_image_backends.py" \
  --output <run-dir>/.pipeline/preflight.json
```

该脚本完整解析 `~/.baoyu-skills/.env`，只报告 provider 是否已配置，绝不输出密钥值。禁止通过 `head`、`grep` 截断配置文件后推断 provider 不存在。

预检只表示“具备尝试条件”，不能冒充图片生成 attempt。真实 fallback 顺序：

1. Skill/EXTEND 当前选中的 backend；
2. `codex-cli` 失败时尝试 `openai-native`；
3. 再尝试 preflight 中其他已配置 provider。

错误分类：

| 情况 | verdict | 动作 |
|---|---|---|
| provider 连接超时/5xx | `network_error` | 换下一个 provider |
| 429/quota | `quota_error` | 换下一个 provider |
| 模型不存在、Codex 版本不兼容、invalid request | `api_error` | 换下一个 provider |
| 输出空、非 PNG、hash 不符 | `empty_output` / `invalid_output` | 换下一个 provider |
| 输入、Skill、EXTEND 或运行上下文缺失 | `contract_error` | 停止并报告 Leader |

Codex CLI 版本或模型不兼容不是 `contract_error`，不得因此跳过已配置的 OpenAI fallback。attempt 的 backend 必须能映射到 `preflight.json` 的已配置 provider；宿主 `imagegen` 适配器规范化为 `openai-native`，可另记 `adapter: imagegen`。

## 7. Manifest 与两阶段验收

Manifest 固定写在 `<run-dir>/.pipeline/manifest.json`，只记录真实发生的事实。最低结构：

```json
{
  "schema_version": 2,
  "protocol_version": "2026-07-13-001",
  "run_id": "...",
  "mode": "newspic",
  "canonical_output_dir": "/abs/run-dir",
  "source": {
    "original_path": "/abs/run-dir/.pipeline/input.md",
    "original_sha256": "...",
    "publisher_text_sha256": "..."
  },
  "skill_contract": {
    "skill_name": "baoyu-xhs-images",
    "skill_path": "/abs/SKILL.md",
    "skill_sha256": "...",
    "files_read": ["/abs/SKILL.md", "/abs/reference.md"],
    "preferences": {
      "source": "user|extend|auto",
      "style": "sketch-notes",
      "extend_path": "/abs/EXTEND.md",
      "extend_sha256": "..."
    }
  },
  "images": [{
    "id": "01",
    "kind": "card",
    "source_skill": "baoyu-xhs-images",
    "prompt_path": "/abs/run-dir/prompts/01-cover-topic.md",
    "prompt_sha256": "...",
    "prompt_written_at": "ISO-8601",
    "output_path": "/abs/run-dir/01-cover-topic.png",
    "output_sha256": "...",
    "aspect": "3:4",
    "attempts": [{
      "scope": "image",
      "backend": "openai-native",
      "prompt_sha256": "...",
      "started_at": "ISO-8601",
      "finished_at": "ISO-8601",
      "verdict": "success",
      "error_summary": ""
    }],
    "status": "success"
  }]
}
```

plan 阶段图片尚未生成，`output_sha256` 可以省略或为 `null`；publish-ready 阶段必须填写真实输出文件的 SHA-256。不得为了补齐 plan 示例而预造 hash 或图片。

每个 image attempt 必须对应真实 prompt、真实 backend 调用，并且 `started_at >= prompt_written_at`。通用 smoke test 只能写进 `preflight.json`。

规划完成后：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/validate_designer_manifest.py" \
  <run-dir>/.pipeline/manifest.json --phase plan
```

发布前：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/validate_designer_manifest.py" \
  <run-dir>/.pipeline/manifest.json --phase publish-ready
```

`publish-ready` 要求每张图均为成功、最后 attempt 为 success、PNG 非空且 hash 匹配。任何 failed 图片都会使整体验收失败。校验器还必须检查 PNG IHDR 尺寸与声明比例一致，并拒绝重复的图片 ID、prompt 路径、输出路径和输出 hash。

### Layout manifest 与排版验收

News Typesetter 固定写入 `.pipeline/layout.json`（schema 1）。它必须记录最终 Markdown 路径/hash、gzh-design Skill 路径/hash、实际读取文件、固定 upstream commit、主题与文章类型决策、`preserve-visible-text` 内容策略、发布元数据和 `article-body.html` 路径/hash。

最低结构：

```json
{
  "schema_version": 1,
  "protocol_version": "2026-07-13-001",
  "run_id": "...",
  "mode": "news",
  "canonical_output_dir": "/abs/run-dir",
  "source": {
    "markdown_path": "/abs/run-dir/article-source.md",
    "markdown_sha256": "...",
    "original_path": "/abs/run-dir/.pipeline/input.md",
    "original_sha256": "..."
  },
  "skill_contract": {
    "skill_name": "gzh-design",
    "skill_path": "/abs/plugin/skills/gzh-design/SKILL.md",
    "skill_sha256": "...",
    "tree_sha256": "...",
    "files_read": ["/abs/SKILL.md", "/abs/theme-index.md", "/abs/theme-x.md", "/abs/common-components.md"],
    "upstream_commit": "..."
  },
  "decision": {
    "theme": "摸鱼绿",
    "theme_source": "user|auto",
    "article_type": "教程/操作指南",
    "content_policy": "preserve-visible-text"
  },
  "metadata": {
    "title": "文章标题",
    "author": "",
    "summary": "文章摘要",
    "cover_path": "/abs/run-dir/cover.png"
  },
  "output": {
    "html_path": "/abs/run-dir/article-body.html",
    "html_sha256": "...",
    "generated_at": "ISO-8601"
  }
}
```

发布前必须运行：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/validate_article_layout.py" \
  <run-dir>/article-body.html \
  --manifest <run-dir>/.pipeline/layout.json \
  --output <run-dir>/.pipeline/layout-validation.json
```

该门禁直接调用内置 gzh-design 的原始 `validate_gzh_html.py`，并追加片段结构、占位符、路径、完整 Skill tree hash、锁定 commit、运行身份和 sealed 原稿可见文本逐段保真检查。文章 H1 由草稿 metadata 承载，不要求重复出现在正文。ERROR 或 WARNING 任一非零都不得进入发布。

## 8. 发布适配

Newspic Publisher 必须通过 `--manifest <run-dir>/.pipeline/manifest.json` 消费 sealed 原文与按 manifest 顺序排列的 Baoyu 原生图片，不接受独立 `--content` 或 `--image` 覆盖。News Publisher 必须消费已验收的 `article-body.html`，不得再次把 Markdown 渲染为 HTML：

- 调用 `publish.py article --html <run-dir>/article-body.html --layout-manifest <run-dir>/.pipeline/layout.json --result-output <run-dir>/.pipeline/publish-result.json --verify-draft`；
- 解析每个 `<img src>`，上传本地/远程正文图并只替换 `src` 为微信 `mmbiz` URL；已是微信图片 URL 的不重复上传；
- 保留 gzh-design 生成的容器、内联样式、图注与顺序，不复制、不重命名原生图片；
- 封面使用 layout manifest 的 `metadata.cover_path` 或用户显式 `--cover`；
- 提交前必须同时通过 designer `publish-ready` 与 layout 验收；草稿创建后必须调用 `draft/get` 回读标题、摘要、可见正文和微信图片 URL。

Publisher 只能替换图片 `src`，不得修改其他正文、重新排版、重新生图或补 manifest。旧 Markdown renderer 仅保留为显式兼容路径，不得在同一 run 中静默 fallback。`draft/add` 成功后必须立即原子写入 `publish-result.json`；恢复时若请求指纹一致且已有 `draft_media_id`，只能复用回执或重试 `draft/get`，不得再次创建草稿。请求指纹不一致时必须停止并交用户判断。

Publisher 在每张素材上传后更新同一原子回执，恢复时复用已上传的素材 ID/URL。`draft/add` 是非幂等写入，禁止任何网络自动重试；若响应结果不确定，必须写入 `creation_status: unknown` 并停止，后续恢复不得再次调用 `draft/add`。人工在草稿箱确认唯一草稿后，可用 `--recover-draft-media-id` 绑定该草稿并强制执行 `draft/get` 验证。

成功回读必须记录 `verification.status: verified`、`method: draft/get` 和 `verified_at`；`skipped`、`pending`、`blocked` 即使误填 `ok: true` 也不得进入 `published`。Newspic 回执必须绑定 manifest、sealed source、按序图片 hash 与上传后的素材 ID；News 回执必须绑定 layout 和最终 HTML hash。

Publisher 回报后，Leader 必须运行 `validate_publish_result.py <run-dir>`。`run_context.py` 本身也拒绝在回执缺失、账号/模式不一致或 `verification.ok` 非真时进入 `published`。

## 9. 失败与恢复

- worker 失败后立即回报 Leader，包含 `run_id`、阶段、真实错误和已完成文件。
- Leader 只在同一 `run_id` 中恢复；不得新建目录或重新生成已经锁定的 prompts。
- 已通过 layout 验收的 `article-body.html` 必须按 hash 复用；只有用户明确要求换主题/修排版时才生成新版本并保留旧 hash 证据。
- provider fallback 不得修改 prompt。需要修改 prompt 时必须作为用户/Leader 明确批准的新规划版本，并保留旧文件。
- 所有 provider 失败时，Leader 返回失败握手；外层调用者不得自行直调 Skill。
- publisher 对可安全重试的读取和素材上传保留 30/60/120 秒退避；非幂等 `draft/add` 永不自动重试。微信业务 errcode 按语义处理。代码重试耗尽后，Agent 不得再次循环整个发布流程。
- `.pipeline/events.jsonl` 只追加记录状态迁移和关键阶段；`run.json` 继续只保存当前状态，恢复判断以两者和确定性产物为证据。

## 10. 完成条件

只有以下条件同时满足才可报告成功：

1. designer `publish-ready` 校验通过；
2. news 模式的 layout 校验 ERROR=0、WARNING=0；
3. `.pipeline/publish-result.json` 已原子落盘且包含草稿 `media_id`；
4. `draft/get` 回读验证 `verification.ok: true`；
5. `run.json` 状态更新为 `published`；
6. Leader 返回终态握手。
