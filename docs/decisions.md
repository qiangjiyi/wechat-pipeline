# 技术决策记录

## ADR-001：使用 gzh-design 作为公众号文章排版阶段

- 状态：已采纳
- 日期：2026-07-12
- 适用版本：wechat-pipeline 0.2.0 / protocol 2026-07-12-001

### 背景

原 article Publisher 在发布时通过 `baoyu-md` 把 Markdown 渲染为 HTML。该路径适合作为通用 Markdown 转换器，但主题表达、公众号专属组件、粘贴兼容规则和最终 HTML 门禁有限。

`isjiamu/gzh-design-skill` 提供六套公众号主题组件库、文章类型配方、纯内联样式、`span leaf` 包裹和最终 HTML 校验。项目所有者已确认本项目可以使用和分发该 Skill；上游许可证和固定版本信息仍完整保留。

### 决策

1. 把固定上游 commit 的 gzh-design 运行快照原样纳入 Plugin，不修改其 `SKILL.md`、references、scripts 或 assets。
2. Formatter 后创建只读 `content.md`；自 protocol `2026-07-18-002` 起，文章配图 Skill 在隔离副本中自然插入图片引用，Typesetter 消费其返回的最终文章，canonical `content.md` 仍保持只读。
3. 新增独立 Typesetter worker，在 Designer 与 Publisher 之间原生执行 gzh-design。
4. News Publisher 默认消费已验收的 `article-body.html`，不再在发布阶段二次渲染 Markdown。
5. Publisher 只上传正文图片并替换 `img[src]`，保留 Typesetter 生成的其余 HTML。
6. 旧 `baoyu-md` Markdown renderer 作为显式兼容路径保留，不允许在同一 run 中静默 fallback。
7. 流水线调用 gzh-design 时采用 `preserve-visible-text` 内容策略：保留全部原文，不自动追加署名、CTA、观点或占位文案。
8. 自 protocol `2026-07-18-006` 起，Typesetter 只在隔离 workspace 中自然调用 gzh-design 并返回最终干净 HTML。`layout.json`、canonical HTML、hash、固定 lock 绑定和验收回执全部由 `prepare_layout.py` 确定性生成；Agent 不再手写或修补这些集成产物。
9. Pipeline 不验收 gzh-design 内部主题、组件、prompt、推理、辅助文件或自然文件名，只验收原生 Skill 调用边界、最终 HTML、原文与图片保留、禁止新增的作者/互动结尾以及发布绑定。

### 运行门禁

- Designer manifest 必须通过 `publish-ready`。
- `article-body.html` 必须通过上游原始 `validate_gzh_html.py`。
- `.pipeline/layout.json` 必须通过项目的 `validate_article_layout.py`，包括运行身份、固定 commit、完整 Skill tree hash、路径、产物 hash、占位符、正文片段和源 Markdown 可见文本逐段保真检查。
- ERROR 或 WARNING 任一非零时不得发布。

### 上游维护

固定版本记录在 `plugins/wechat-pipeline/third_party/gzh-design.lock.json`。更新时只允许通过 `scripts/sync_gzh_design_skill.py` 从工作区干净的官方本地 clone 同步，并在提交前核对整树 SHA-256、测试和 Plugin manifest。

## ADR-002：发布回执优先于终态，状态只由 Leader 写入

- 状态：已采纳
- 日期：2026-07-13
- 适用版本：wechat-pipeline 0.3.0 / protocol 2026-07-13-001

### 背景

微信 `draft/add` 成功后，如果 Publisher 在向 Leader 回报前中断，只有对话消息能证明草稿已创建。恢复执行可能重复创建草稿。同时，旧协议允许 worker 与 Leader 都推进 `run.json`，跨宿主执行时状态所有权不够明确。

### 决策

1. `draft/add` 返回后立即原子写入 `.pipeline/publish-result.json`，保存请求指纹和 `draft_media_id`。
2. 发布后使用 `draft/get` 回读标题、摘要、正文和图片，验证结果写回同一回执。
3. 恢复时若同一请求指纹已有 `draft_media_id`，禁止再次调用 `draft/add`，只允许复用回执或重试回读。
4. 只有 Leader 可以调用 `run_context.py status`；Formatter、Designer、Typesetter、Publisher 只产出文件和证据。
5. `run_context.py` 把状态迁移追加写入 `.pipeline/events.jsonl`，并在回执未验证时拒绝进入 `published`。
6. 所有 Plugin Python 脚本通过 `run_python.sh` 自动选择 Python 3.10+，避免系统默认解释器过旧时误报环境不可用。

### 补充约束

- `draft/add` 不具备幂等语义，禁止网络自动重试；不确定结果写入 `creation_status: unknown` 并阻止自动重建。
- Newspic 发布从 manifest 读取 sealed 原文和有序图片，发布回执保存上游 hash 与上传素材 ID。
- 状态机按模式隔离，回读验收必须明确记录 `draft/get`、`verified` 和验证时间。

## ADR-003：控制面与内容面分离，只保留单阶段图片并行

- 状态：已采纳
- 日期：2026-07-18
- 适用版本：wechat-pipeline 0.5.0 / protocol 2026-07-18-001

### 背景

跨阶段提前排版和并发验证在没有 revision、锁和不可变快照时造成 manifest 覆盖、状态提前、别名图片和失败后继续发布。Agent 还可以修改 validator 或 trust lock 来让自身产物通过。

### 决策

1. 状态、门禁、运行时完整性和发布快照全部由确定性脚本控制。
2. Formatter、Designer、Typesetter、Publisher 只写各自拥有的内容产物。
3. `content.md` 与 `publish-snapshot.json` 创建后只读。
4. Typesetter 必须等待全部图片通过 artwork gate。
5. 此条自 ADR-004 起由“视觉 Skill 自主管理内部并发”取代；Pipeline 不再拆分或重排单图生成任务。
6. Publisher 必须验证 snapshot，发布回执必须绑定 snapshot hash 和 fingerprint。
7. `run.json` 增加 revision 与 state checksum，拒绝直接状态编辑。

### 结果

牺牲约 1–2 分钟的跨阶段重叠，换取可重放、可恢复且不可通过普通 Agent 操作绕过的发布链路。主要性能收益来自图片 batch，而不是让多个 Agent 同时写共享文件。

## ADR-004：Pipeline 只编排原生 Skill，不编排 Skill 内部工作流

- 状态：已采纳
- 日期：2026-07-18
- 适用版本：protocol 2026-07-18-002

### 背景

真实运行显示 Designer 绕过 `baoyu-cover-image` 和 `baoyu-article-illustrator`，自行写简化 prompt，再用 EXTEND 文件和宽松 manifest 模拟 Skill 已执行。旧的 planning/rendering 协议同时强制 prompt 位置、图片数量和 backend，迫使 Pipeline 重建原生 Skill 已经拥有的工作流。

### 决策

1. 合并 planning/rendering 为单一 `designing` 状态。
2. Designer 只通过运行时原生机制调用模式对应的视觉 Skill，不生成替代 prompt 或图片。
3. 每个 Skill 在运行根目录下独立的 `<invocation-id>/` 中完整执行自己的当前流程，不增加 `skill-output/` 包装层；具体输出子目录继续由该 Skill 的 `EXTEND.md` 决定。
4. Pipeline 不校验 outline、prompt、references、视觉决策、图片密度或内部 backend。
5. 控制面只绑定准确的 `SKILL.md`、输入、开始/完成状态和 Skill 明确返回的最终结果。
6. News Typesetter 使用文章配图 Skill 返回的最终文章，保留其图片落位决定。

### 结果

Pipeline 成为薄编排层，视觉能力和工作流继续由可独立升级的原生 Skill 所有。目标 Skill 失败时流程停止，不再存在手工模拟 Skill 的降级路径。

## ADR-005：统一 Skill 边界并并行隔离的视觉工作

- 状态：已采纳
- 日期：2026-07-19
- 适用版本：wechat-pipeline 0.8.0 / protocol 2026-07-20-001

### 决策

1. Formatter、visual、layout 共用 `skill_run.py` 的 start/complete/fail/reset 生命周期，role 白名单由该脚本单源维护。
2. complete 负责输出路径、hash、时间、role 数量与源文保真检查；错误消息必须附带可复制的完整命令。
3. 失败恢复只能由 Leader reset 并记录事件；历史 role 纠正只能使用 `amend-role`。
4. release/runtime 完整性合并，状态前进恢复运行时门禁；同一 run 用文件数、最新 mtime 与总大小跳过未变化的重复全树 hash，发布前和 published 强制重算。
5. News 的封面和正文配图没有数据依赖，使用两个隔离 Worker 并行；二者完成后才构建唯一 manifest。
6. 图片 batch 的显式 CLI provider 高于 task/EXTEND/环境默认，`codex-cli` 默认并发度为 2。

### 结果

契约错误、跨阶段返工和 Leader 手工救火被确定性边界前置拦截；主要墙钟由并行后的正文图片生成决定。首次真实运行仍需记录墙钟、hash 次数和 token，验证目标值而不是由代码测试推断。

## ADR-006：入口主线程统一承担 Leader 调度

- 状态：已采纳
- 日期：2026-07-21
- 适用版本：wechat-pipeline 0.8.1 / protocol 2026-07-21-001

### 背景

Claude Code 入口曾先派发 `wechat-leader`，再要求该子 Agent 派发 Formatter 等 Worker。飞书经 AAMP/ACP 触发的真实运行证明，子 Agent 不具备可靠的再次派工能力，流程会在创建 formatter 调用边界后停滞。

### 决策

1. Claude Code 与 Codex 都由顶层 `wechat-pipeline` Skill 所在主线程作为唯一 Leader。
2. Formatter、Designer、Typesetter、Publisher 均由入口主线程直接派发，保持第一层 Worker 拓扑。
3. 删除 `wechat-leader` Agent；保留 `wechat-leader` 作为确定性脚本的逻辑 actor 名，避免迁移既有状态和审计合同。
4. Worker 继续禁止派发子 Agent，视觉并行、恢复与门禁规则不变。

### 结果

宿主调度拓扑与“子 Agent 不得再派子 Agent”的协议一致，飞书入口不再依赖不可用的嵌套 Agent 能力；Claude Code 与 Codex 的控制流程也不再分叉。
