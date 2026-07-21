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
- 每次完整 Pipeline 都必须执行原生 Formatter，不设置基于原稿结构的跳过分支。
- 输出：自然格式化结果。
- 允许：由原生 Skill 自然增加 Markdown 结构和必要 frontmatter，不规定 H2/H3 数量。
- 禁止：改写观点、生成图片、修改控制文件。
- 返回前先执行只读 check-only；诊断精确返回原稿行号和预览，修复仍由同一 Formatter 完成。
- 自然产物固定为 `baoyu-format-markdown/article-formatted.md`；完成时统一边界脚本先校验源文保真，再由控制面生成 canonical `content.md` 与 `format-result.json`，拒绝 sealed 原稿直接进入门禁。

### Designer

- 输入：已验证的 `content.md`。
- 在单一 `designing` 阶段完整调用模式对应的原生视觉 Skill；news 的封面与正文配图由两个隔离 Worker 并行执行。
- `skill_run.py --boundary visual start` 生成发布场景选项；news 封面固定返回 `aspect: 2.35:1`，Designer 只负责原样转交。
- 每个 Skill 使用独立 workspace，自主决定分析、确认、prompt、图片数量、风格和 backend。
- Designer 只登记 Skill 明确返回的最终结果；不得手工模拟或降级替代 Skill。
- manifest 由 `skill_run.py --boundary visual build-manifest` 根据完成记录确定性生成。

### Typesetter

- 仅 news。
- 只有 artwork gate 通过后才能启动。
- 输入：原生配图 Skill 返回的最终配图文章与 designer manifest。
- 自然输出：隔离 workspace 中由 `gzh-design` 返回的最终干净 HTML。
- 集成输出：Leader 调用 `prepare_layout.py` 确定性生成 `article-body.html`、`layout.json` 和 `layout-validation.json`。
- HTML 只能引用 manifest 中声明的原始正文图片路径。

### Publisher

- 输入仅为 `.pipeline/publish-snapshot.json`。
- 不重新运行 formatter、designer 或 typesetter。
- 不修改上游文件。
- 负责素材上传、一次 `draft/add`、`draft/get` 回读和持久化回执。

## Skill 设计

- `wechat-pipeline`：保持为轻量入口，只做宿主路由和控制面调用。
- `baoyu-format-markdown`：只负责格式化内容。
- `baoyu-xhs-images`：完整负责 newspic 从分析到生图的原生流程。
- `baoyu-cover-image`：完整负责 news 封面从分析到生图的原生流程。
- `baoyu-article-illustrator`：完整负责 news 正文配图分析、生图和文章图片引用落位。
- 图片 backend、并发和 fallback 属于上述 Skill 内部决策，Pipeline 不直接编排 `baoyu-image-gen`。
- `gzh-design`：只负责 news HTML 排版。
- `wechat-publisher`：只负责消费 publish snapshot 并发布。

Skill 中保留完整内容工作流和领域知识；脚本只实现调用边界、输入绑定、发布场景选项、状态、最终结果 hash 和发布门禁，不描述或验证 Skill 内部步骤。News 封面 `2.35:1` 是下游发布合同，不是 Pipeline 重建封面 Skill 工作流。

## 状态机

```text
input_sealed
  -> formatting
  -> content_ready
  -> designing
  -> artwork_ready
  -> [typesetting -> layout_ready]  # news only
  -> publish_ready
  -> publishing
  -> published
```

任一活动阶段可进入 `failed`；恢复只能回到 `failed_from`。`published` 和 `cancelled` 为终态。

Formatter、visual、layout 三类原生 Skill 使用同一 `skill_run.py` 生命周期。失败回执只能由 Leader 显式 `reset`：清理并重建该 Skill 的隔离 workspace、增加 reset 计数并追加审计事件；历史 role 错误只能通过 `amend-role` 修复，禁止手改回执。

每次前进由 `run_context.py status` 在文件锁内完成，并自动执行目标状态对应的验证器。Leader 无法使用 `--actor` 字符串绕过门禁。

## 产物所有权

| 产物 | 唯一写者 |
|---|---|
| `.pipeline/run.json`、`events.jsonl` | 控制面 |
| `baoyu-format-markdown/article-formatted.md` | Formatter |
| `.pipeline/formatter-skill-run.json` | Formatter Skill 运行记录脚本 |
| `content.md`、`format-result.json` | 内容准备脚本 |
| `<invocation-id>/` 自然产物 | 对应原生视觉 Skill |
| `.pipeline/skill-runs/*.json`、`manifest.json` | 原生 Skill 运行记录脚本 |
| `gzh-design/**`、`layout-skill-run.json` | Typesetter / gzh-design |
| `article-body.html`、`layout.json`、`layout-validation.json` | `prepare_layout.py` |
| `publish-snapshot.json` | snapshot builder |
| `publish-result.json` | Publisher |

禁止通过复制或改名制造第二份等价产物。

## Canonical 目录

```text
<run-dir>/
├── content.md
├── <invocation-id>/...           # Skill 自然产物，不规定内部结构
├── gzh-design/...                # news 排版自然产物
├── article-body.html             # news only
└── .pipeline/
    ├── input.md
    ├── formatter-skill-run.json / runtime-integrity.json / integrity-cache.json
    ├── backends.json
    ├── skill-runs/<invocation-id>.json
    ├── run.json / events.jsonl
    ├── format-result.json / manifest.json / layout.json
    └── publish-snapshot.json / publish-result.json
```

运行根目录下的 Skill 同名目录直接保留原生自然产物，便于检查和复用；`.pipeline` 只保存控制面证据。Pipeline 不搬运、重写或统一 Skill 内部 prompt、outline 等文件。workspace 是独占 article-dir 与安全边界，Skill 仍按自己的 `EXTEND.md/default_output_dir` 决定实际子目录；成功后删除未修改的冗余输入副本。

发布包本身由 `release-integrity.json` 绑定完整文件集合与 hash；一次运行再由 `runtime-integrity.json` 冻结该安装态。前者阻止 Agent 在 init 前修改缓存并把篡改版本“洗白”，后者阻止 init 后修改。release manifest 只能由仓库级维护脚本 `scripts/build_release_integrity.py` 生成，不随安装包提供重签名入口。

## 安全并行

Pipeline 不对视觉 Skill 内部的 batch 和任务分配做二次编排。每个原生 Skill 依据自己的当前说明、EXTEND 和 init 缓存的 backend 能力决定内部策略；news 的封面与正文配图 Skill 在互不共享 workspace 的两个 Worker 中并行，Typesetter 等待二者全部成功。`codex-cli` 图片后端默认并发度为 2，仍可由环境或 EXTEND 显式覆盖。

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
- 视觉阶段：newspic 1 个 Designer；news 2 个隔离 Designer 并行，内部 batch 由原生 Skill 决定。
- 排版：1 个 Typesetter，目标 2–5 分钟。
- 发布与回读：目标 1–3 分钟，不含微信网络异常。
- news 正常目标：≤15 分钟（并发插图）；newspic 正常目标：≤15 分钟。

速度指标服从正确性：不通过减少校验、提前发布或伪造降级换取时长。
