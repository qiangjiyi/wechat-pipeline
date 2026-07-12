---
protocol_version: 2026-07-11-002
protocol_status: active
authority: single-source-of-truth
---

# 微信发布流水线协议

本文档是 Claude Code 与 Codex 两种宿主共同使用的唯一运行协议。与 Agent/Skill 摘要、README 或调用方附加说明冲突时，以本文档为准。

本协议随 `wechat-pipeline` Plugin 安装：

- Claude Code 使用 `${PIPELINE_ROOT}/agents/` 中的四个原生 Agent；Codex 使用 `wechat-pipeline` 主 Skill 调度三个子 Agent。
- 五个经过固定上游 commit 校验的 Baoyu Skill 内置于 `${PIPELINE_ROOT}/skills/`，两个宿主读取同一份原文。
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
protocol_version: 2026-07-11-002
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
python3 "${PIPELINE_ROOT}/scripts/run_context.py" init \
  --mode <newspic|news> \
  --account <account> \
  --slug <ascii-slug> \
  [--source /absolute/path/to/source.md]
```

- `newspic` 默认落在 `${WECHAT_PIPELINE_EXPORTS_DIR:-$HOME/wechat-pipeline-exports}/image-cards/<slug>-<run_id>/`。
- `news` 默认落在 `${WECHAT_PIPELINE_EXPORTS_DIR:-$HOME/wechat-pipeline-exports}/wechat-articles/<slug>-<run_id>/`。
- 命令返回的目录是本次运行唯一的 `canonical_output_dir`。
- worker 只能在该目录内写文件，禁止创建平行目录。
- 重试、fallback、恢复执行全部复用同一个 `run_id`、目录和已落盘 prompt。

如果输入来自聊天而不是本地文件：

1. Leader 先创建权限为 `0600` 的临时文件，并通过宿主文件写入工具逐字写入用户原文；禁止用 shell 插值正文。
2. 把临时文件的绝对路径作为 `init --source` 输入，使脚本在创建目录前即可计算 hash 和查找 reusable run。
3. 用 `try/finally` 包裹初始化，无论 `init` 成功、失败或被中断都删除临时文件。正常流水线不得先创建 `awaiting_input` 运行再补正文；`seal` 子命令只保留给旧运行恢复。

`.pipeline/input.md` 是不可变原始输入。任何发布适配文件都不能冒充原始输入。

创建或复用已 seal 输入的运行后，Leader 必须运行：

```bash
python3 "${PIPELINE_ROOT}/scripts/plugin_doctor.py" \
  --mode <newspic|news> --account <account> \
  --output <run-dir>/.pipeline/doctor.json
```

Doctor 未通过时停止派工并报告缺失配置。不得读取或输出真实密钥值。

Doctor 通过后，状态必须按实际阶段推进：

```text
input_sealed -> planning -> rendering -> ready -> publishing -> published
```

- Leader 在派第一个 worker 前设为 `planning`。
- Designer 通过 plan 校验后设为 `rendering`，通过 publish-ready 校验后设为 `ready`。
- Publisher 调 Skill 前设为 `publishing`，成功后设为 `published`。
- 任一活动阶段失败可设为 `failed`；恢复时回到失败前对应的活动阶段。
- `published` 和 `cancelled` 是终态，不得回退。

## 3. 模式与角色

- 用户明确说贴图、卡片、newspic、图片消息：`newspic`。
- 用户明确说长文、文章、news：`news`。
- 无显式信号时，超过 1200 字符或已是结构化 markdown：`news`；否则 `newspic`。

路径：

```text
newspic: Leader -> designer -> publisher
news:    Leader -> [formatter when input needs formatting] -> designer -> publisher
```

Leader 只负责识别、派工、验收、恢复和汇报，不亲自写 prompt、生图或发布。
当 sealed input 已经具有可用 Markdown 标题/frontmatter 时，Leader 记录 formatter skipped 并直接派 designer；不得为了保持固定链路而空跑 formatter。

Formatter 跳过时使用明确回报：`skipped: true`、`reason: already_structured_markdown`、`skill_files_read: []`、`natural_output_path: <run-dir>/.pipeline/input.md`。该路径表示复用 sealed input，不表示 Formatter 生成了新产物。

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
├── input.md
├── preflight.json
├── manifest.json
└── publish-source.md   # 仅 publisher 需要适配输入时创建
```

这些是运行证据，不是 Baoyu 原生产物。不得把它们伪装成 Skill 产物。

## 6. Provider 预检与 fallback

在生成 prompt 前运行：

```bash
python3 "${PIPELINE_ROOT}/scripts/preflight_image_backends.py" \
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

Codex CLI 版本或模型不兼容不是 `contract_error`，不得因此跳过已配置的 OpenAI fallback。

## 7. Manifest 与两阶段验收

Manifest 固定写在 `<run-dir>/.pipeline/manifest.json`，只记录真实发生的事实。最低结构：

```json
{
  "schema_version": 2,
  "protocol_version": "2026-07-11-002",
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
python3 "${PIPELINE_ROOT}/scripts/validate_designer_manifest.py" \
  <run-dir>/.pipeline/manifest.json --phase plan
```

发布前：

```bash
python3 "${PIPELINE_ROOT}/scripts/validate_designer_manifest.py" \
  <run-dir>/.pipeline/manifest.json --phase publish-ready
```

`publish-ready` 要求每张图均为成功、最后 attempt 为 success、PNG 非空且 hash 匹配。任何 failed 图片都会使整体验收失败。

## 8. 发布适配

Publisher 优先直接消费 Baoyu 原生图片路径。若内置 `wechat-pipeline:wechat-publisher` Skill 需要 frontmatter 输入，可在 `.pipeline/publish-source.md` 创建最小适配文件：

- 正文必须与 `.pipeline/input.md` 字节或规范化正文 hash 一致；
- 图片列表直接引用原生图片，不复制、不重命名；
- 适配文件不是 Baoyu Skill 产物；
- 发布前必须通过 `publish-ready` 校验。

Publisher 不得修改正文、重新生图或补 manifest。

## 9. 失败与恢复

- worker 失败后立即回报 Leader，包含 `run_id`、阶段、真实错误和已完成文件。
- Leader 只在同一 `run_id` 中恢复；不得新建目录或重新生成已经锁定的 prompts。
- provider fallback 不得修改 prompt。需要修改 prompt 时必须作为用户/Leader 明确批准的新规划版本，并保留旧文件。
- 所有 provider 失败时，Leader 返回失败握手；外层调用者不得自行直调 Skill。
- publisher 代码对网络层失败内置 30/60/120 秒重试；微信业务 errcode 按语义处理。代码重试耗尽后，Agent 不得再次循环整个发布流程。

## 10. 完成条件

只有以下条件同时满足才可报告成功：

1. `publish-ready` 校验通过；
2. publisher 返回成功；
3. 获得草稿 `media_id`；
4. `run.json` 状态更新为 `published`；
5. Leader 返回终态握手。
