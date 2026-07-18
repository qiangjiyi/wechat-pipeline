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
2. Formatter 后创建只读 `content.md`；Designer 和 Typesetter 共同读取它，不允许再向正文插入或改名图片引用。
3. 新增独立 Typesetter worker，在 Designer 与 Publisher 之间原生执行 gzh-design。
4. News Publisher 默认消费已验收的 `article-body.html`，不再在发布阶段二次渲染 Markdown。
5. Publisher 只上传正文图片并替换 `img[src]`，保留 Typesetter 生成的其余 HTML。
6. 旧 `baoyu-md` Markdown renderer 作为显式兼容路径保留，不允许在同一 run 中静默 fallback。
7. 流水线调用 gzh-design 时采用 `preserve-visible-text` 内容策略：保留全部原文，不自动追加署名、CTA、观点或占位文案。

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
5. 唯一允许的并行是同一 Designer 对不同图片进行 batch 调用；共享 manifest 最后单次汇总。
6. Publisher 必须验证 snapshot，发布回执必须绑定 snapshot hash 和 fingerprint。
7. `run.json` 增加 revision 与 state checksum，拒绝直接状态编辑。

### 结果

牺牲约 1–2 分钟的跨阶段重叠，换取可重放、可恢复且不可通过普通 Agent 操作绕过的发布链路。主要性能收益来自图片 batch，而不是让多个 Agent 同时写共享文件。
