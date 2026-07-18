---
protocol_version: 2026-07-18-001
protocol_status: active
authority: single-source-of-truth
---

# 微信发布流水线协议 V2

本协议同时约束 Claude Code Agent 和 Codex Skill。目标是把一份原始草稿可靠地转换为一个经过微信草稿回读验证的产物。

## 1. 不可违反的原则

1. 每次请求只有一个 `run_id`、一个 canonical 目录和一个 Leader。
2. Agent 负责内容工作；确定性脚本负责状态、hash、门禁、幂等和发布快照。
3. Leader 不写 formatter、designer、typesetter 或 publisher 的产物。
4. Worker 只能写 canonical 目录，禁止修改 Plugin、Skill、validator 和 trust lock。
5. 校验失败只能修复同一阶段的真实产物，不能补造证据、placeholder、空白图或第二套目录。
6. Publisher 只能消费只读 `.pipeline/publish-snapshot.json`。
7. `draft/get` 回读验证是成功的必要条件；创建草稿不等于发布成功。

## 2. 模式

- `newspic`：格式化原稿 → 生成 1–20 张 3:4 图片卡片 → 发布。
- `news`：格式化原稿 → 生成 1 张 2.35:1 封面和至少 1 张 16:9 正文图 → gzh-design 排版 → 发布。

用户明确模式优先。无显式信号时，长文或结构化 Markdown 使用 `news`，短内容使用 `newspic`。

## 3. 状态机

```text
input_sealed
  -> formatting
  -> content_ready
  -> planning
  -> rendering
  -> artwork_ready
  -> [typesetting -> layout_ready]  # 仅 news
  -> publish_ready
  -> publishing
  -> published
```

活动阶段可进入 `failed`，恢复只能回到 `failed_from`。`published` 和 `cancelled` 不可回退。

所有状态只能由 Leader 调用：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/run_context.py" \
  status <run-dir> <target> --actor wechat-leader
```

`run_context.py` 会在文件锁内自动执行目标状态对应的门禁。禁止直接编辑 `.pipeline/run.json`。

## 4. 初始化与运行时完整性

正常初始化必须携带 source：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/run_context.py" init \
  --mode <newspic|news> --account <account> --slug <ascii-slug> \
  --source <absolute-source-path>
```

- 聊天正文先逐字写入权限 `0600` 的临时文件，并在 `finally` 删除。
- `.pipeline/input.md` 为权限 `0400` 的 sealed 原稿。
- `init` 同时写入 `.pipeline/runtime-integrity.json`，冻结当前 Agent、脚本、publisher、gzh-design 和 trust lock 的 hash。
- 后续任一门禁发现运行时代码变化，立即停止。
- `seal` 只用于旧版孤儿运行恢复，正常流程不得创建 `awaiting_input`。

初始化后必须运行 `plugin_doctor.py`。Doctor 未通过时不派 Worker。

## 5. 格式化

所有模式先进入 `formatting`。

- 输入：`.pipeline/input.md`。
- 已有且有效的 Markdown 结构时可以跳过 LLM Formatter。
- 否则 Formatter 原生执行 `baoyu-format-markdown`，只增加标题层级、列表、引用和必要 frontmatter，不改写原意。
- Leader 对 Formatter 自然产物调用：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/prepare_content.py" \
  <run-dir> --source <formatter-output-or-sealed-input>
```

脚本固定生成只读 `<run-dir>/content.md` 和 `.pipeline/format-result.json`，验证：

- 恰好一个 H1；
- 原稿所有可见文本均保留；
- source、content 和 receipt hash 一致。

通过后才允许进入 `content_ready`。

## 6. 图片设计与生成

Designer 读取只读 `content.md`，分两个阶段使用同一个逻辑 Worker：

### Planning

- newspic：执行 `baoyu-xhs-images`，固定 aspect 3:4。
- news 封面：执行 `baoyu-cover-image`，固定 aspect 2.35:1。
- news 正文图：执行 `baoyu-article-illustrator`，固定 aspect 16:9。
- 用户未明确时，风格、调色、布局和图片数量由原生 Skill/EXTEND 决定。
- 每个 prompt 必须先落盘，再记录 prompt hash 和真实写入时间。
- `.pipeline/manifest.json` 必须包含 `plan.image_count` 及模式对应数量。

Leader 运行 `validate_designer_manifest.py --phase plan`；通过后进入 `rendering`。

### Rendering

- 执行统一的 `baoyu-image-gen` backend 链。
- 允许同一 Designer 在一次 batch 中并行生成不同图片；默认并发上限 3。
- 每张图使用独立 output 和 attempt，不能并发写共享 manifest。
- batch 完成后由 Designer 单次汇总 manifest。
- fallback 必须复用相同 prompt hash。
- 所有 provider 失败时整张图片失败，不允许 placeholder 降级。

`publish-ready` 图片门禁至少验证：

- 真实配置 backend 和真实 attempt 时间顺序；
- prompt、output、hash、完整 PNG chunk/CRC/像素流，并拒绝纯色或全透明空白图；
- PNG 至少 4096 字节；
- newspic 最小 900×1200、aspect 3:4；
- news 封面最小 900×380、aspect 2.35:1；
- news 正文图最小 1200×675、aspect 16:9；
- newspic 1–20 张 card；news 恰好 1 张 cover 且至少 1 张正文图；
- 不允许重复 ID、路径或图片 hash。

通过后进入 `artwork_ready`。

## 7. 排版

仅 news 执行。Typesetter 必须等 `artwork_ready` 后启动；V2 不允许提前排版或 pending-image 占位。

- 输入：只读 `content.md` 与 publish-ready designer manifest。
- 完整执行固定快照 `gzh-design`。
- 输出：`article-body.html`、`.pipeline/layout.json` 和 `.pipeline/layout-validation.json`。
- `preserve-visible-text`：不改写、不删减、不追加原文没有的 CTA 或观点。
- HTML 每个本地正文图片必须使用绝对路径，并与 designer manifest 中非 cover 图片逐项、按序完全一致。
- `layout.metadata.cover_path` 必须精确等于 manifest cover 输出。
- ERROR 或 WARNING 任一非零都不能进入 `layout_ready`。

## 8. 发布快照

artwork（newspic）或 layout（news）通过后，Leader 运行：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/build_publish_snapshot.py" \
  <run-dir>
```

`.pipeline/publish-snapshot.json` 绑定：

- run、账号、模式、sealed source、content、format receipt；
- designer manifest 和有序图片 path/hash；
- news 的 layout、HTML、cover 和正文图片；
- runtime hash 和 validator hash；
- snapshot fingerprint。

快照创建后只读。任一上游文件变化都会使 Publisher 拒绝执行。快照验证通过后进入 `publish_ready`，再进入 `publishing`。

## 9. 发布与恢复

Publisher 固定使用：

```bash
# newspic
publish.py newspic --manifest <manifest> --snapshot <publish-snapshot> \
  --result-output <publish-result> --verify-draft

# news
publish.py article --html <article-body.html> --layout-manifest <layout> \
  --snapshot <publish-snapshot> --result-output <publish-result> --verify-draft
```

- Publisher 不修改任何上游文件。
- 素材上传可安全重试并持久化检查点。
- `draft/add` 是非幂等调用，永不自动重试。
- 请求指纹一致且已有 `draft_media_id` 时，只能重试 `draft/get`。
- `draft/add` 结果不确定时写 `creation_status: unknown` 并停止。
- 回读必须验证标题、正文可见文本、图片数量和微信图片 URL。

Leader 最后运行 `validate_publish_result.py`。只有 receipt 与 snapshot 完整绑定且 `verification.ok: true` 时，才能进入 `published`。

## 10. Worker 与会话

- 一个 run 的每个角色最多一个逻辑 Worker。
- 失败后恢复该 Worker；不能用新 Session 创建第二个 run。
- Worker 派工必须包含 protocol、`PIPELINE_ROOT`、run_id、canonical 目录、当前状态和允许写入的产物。
- 子 Agent 不得再派子 Agent。
- 禁止超过 10 秒的 sleep。优先使用宿主 wait/事件机制；必要轮询每 5 秒一次。

## 11. 完成条件

只有以下条件同时满足才可向用户报告成功：

1. format、designer、layout（news）门禁全部通过；
2. publish snapshot 当前有效；
3. `publish-result.json` 已持久化并绑定 snapshot；
4. `draft/get` 回读验证成功；
5. run 状态为 `published`；
6. Leader 返回唯一终态握手。

```text
WECHAT_PIPELINE_RESULT
protocol_version: 2026-07-18-001
run_id: <run_id>
canonical_output_dir: <absolute path>
status: published | failed | blocked
owner: wechat-leader
next_action: report_to_user | resume_same_leader
direct_skill_fallback_allowed: false
```
