# WeChat Pipeline V2 架构

## 第一性原理

流水线的价值不是“让多个 Agent 都参与”，而是把一份原稿可靠地变成一个高质量微信草稿。LLM 擅长内容判断和视觉创作；状态、hash、幂等、并发和发布副作用必须交给确定性程序。

因此 V2 使用两层架构：

```text
控制面（确定性脚本，唯一状态写者）
  ├─ run/context + stage gate
  ├─ artifact validators
  ├─ publish snapshot
  └─ receipt/read-back audit

内容面（Agent + 原生 Skills）
  ├─ Formatter
  ├─ Designer
  ├─ Typesetter（仅 news）
  └─ Publisher
```

## Agent 设计

### Leader

- 只解析用户意图、调用控制面、派发或恢复 worker、向用户回报。
- 不写 Markdown、prompt、manifest、HTML、图片或发布回执。
- 不直接调用图片 API 和微信 API。
- 每个 run 对每个角色只维护一个逻辑 worker。

### Formatter

- 输入：只读 `.pipeline/input.md`。
- 输出：自然格式化结果。
- 允许：增加 Markdown 结构和必要 frontmatter。
- 禁止：改写观点、生成图片、修改控制文件。
- 完成后由控制面生成并验证 canonical `content.md` 与 `format-result.json`。

### Designer

- 输入：已验证的 `content.md`。
- planning 阶段只写 prompt 和计划事实。
- rendering 阶段只生成图片及 per-image attempt receipt。
- batch worker 不直接汇总“成功”；Designer 在任务结束后单次汇总 manifest，控制面独立验收。
- news：1 张封面与正文图；newspic：3:4 卡片。

### Typesetter

- 仅 news。
- 只有 artwork gate 通过后才能启动。
- 输入：冻结的 `content.md` 与 designer manifest。
- 输出：`article-body.html`、`layout.json`。
- HTML 只能引用 manifest 中声明的原始正文图片路径。

### Publisher

- 输入仅为 `.pipeline/publish-snapshot.json`。
- 不重新运行 formatter、designer 或 typesetter。
- 不修改上游文件。
- 负责素材上传、一次 `draft/add`、`draft/get` 回读和持久化回执。

## Skill 设计

- `wechat-pipeline`：保持为轻量入口，只做宿主路由和控制面调用。
- `baoyu-format-markdown`：只负责格式化内容。
- `baoyu-xhs-images`：只负责 newspic 视觉规划。
- `baoyu-cover-image`：只负责 news 封面规划。
- `baoyu-article-illustrator`：只负责 news 正文配图规划。
- `baoyu-image-gen`：作为所有图片的统一执行后端；并发发生在它的 batch executor 内。
- `gzh-design`：只负责 news HTML 排版。
- `wechat-publisher`：只负责消费 publish snapshot 并发布。

Skill 中保留内容工作流和领域知识；所有脆弱契约都由脚本实现，避免在多个 Agent 提示词中重复。

## 状态机

```text
input_sealed
  -> formatting
  -> content_ready
  -> planning
  -> rendering
  -> artwork_ready
  -> [typesetting -> layout_ready]  # news only
  -> publish_ready
  -> publishing
  -> published
```

任一活动阶段可进入 `failed`；恢复只能回到 `failed_from`。`published` 和 `cancelled` 为终态。

每次前进由 `run_context.py status` 在文件锁内完成，并自动执行目标状态对应的验证器。Leader 无法使用 `--actor` 字符串绕过门禁。

## 产物所有权

| 产物 | 唯一写者 |
|---|---|
| `.pipeline/run.json`、`events.jsonl` | 控制面 |
| `content.md`、`format-result.json` | 内容准备脚本 |
| prompt、单图输出、单图 attempt | Designer |
| `.pipeline/manifest.json` | Designer 单一汇总阶段 |
| `article-body.html`、`layout.json` | Typesetter |
| `publish-snapshot.json` | snapshot builder |
| `publish-result.json` | Publisher |

禁止通过复制或改名制造第二份等价产物。

## 安全并行

V2 首版只允许一种并行：同一份已冻结图片计划中，不同图片的生成调用并行执行。

约束：

- 每张图片有独立 prompt、输出路径和 attempt 文件。
- 并行 worker 不写共享 manifest。
- 所有任务结束后由单一汇总器构建 manifest。
- Typesetter 不与 Designer 并发；layout validation 不与 artifact 写入并发。
- Provider 并发上限默认 3，可配置但不能超过计划图片数。

这样保留主要速度收益，同时消除跨阶段竞态。待 revision/CAS 和真实压测证明安全后，再考虑其他阶段重叠。

## 发布快照

`publish-snapshot.json` 是发布唯一输入，包含：

- run、账号、模式和 source hash；
- format receipt hash；
- designer manifest hash 和有序图片 path/hash；
- news 的 layout、HTML、封面和正文图片 path/hash；
- 所有 validator 的代码 hash 与结果；
- snapshot 自身 fingerprint。

快照创建后，上游文件发生任何变化都会使 Publisher 拒绝运行。

## 性能目标

- 格式化：1 个 worker，会话复用。
- 图片规划：1 个 Designer 规划回合。
- 图片生成：batch 并发，目标 3–8 分钟。
- 排版：1 个 Typesetter，目标 2–5 分钟。
- 发布与回读：目标 1–3 分钟，不含微信网络异常。
- news 正常目标：10–20 分钟；newspic 正常目标：8–15 分钟。

速度指标服从正确性：不通过减少校验、提前发布或伪造降级换取时长。
