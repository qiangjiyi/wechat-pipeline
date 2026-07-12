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
2. Formatter 后创建一次可写的 `article-source.md`；Baoyu article-illustrator 在该副本中原生插入图片引用，不接触只读 sealed 原稿。
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
