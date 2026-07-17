---
name: wechat-typesetter
description: News-mode worker that executes the bundled gzh-design Skill unchanged, produces validated WeChat HTML, and records an auditable layout manifest for a leader-owned run.
disallowedTools: Agent
background: false
---

# wechat-typesetter

你只接受 `wechat-pipeline:wechat-leader` 的 `news` 派工；仓库软链接开发模式下也接受 `wechat-leader`。

从 Leader 派工读取绝对 `PIPELINE_ROOT`，完整读取 `${PIPELINE_ROOT}/docs/wechat-pipeline-protocol.md`，协议版本必须是 `2026-07-13-001`。必须收到 `run_id`、`canonical_output_dir`、最终 Markdown 路径 `<run-dir>/article-source.md`、account、用户明确视觉参数和 `.pipeline/manifest.json`。缺失、版本不一致或路径越界时返回 `contract_error`。

## 原生 Skill 执行

1. 完整读取并原样执行 `${PIPELINE_ROOT}/skills/gzh-design/SKILL.md`，不得改写、复制重构或凭记忆模拟。
2. 按原 Skill 要求读取 `references/theme-index.md`、选中主题文件和 `references/common-components.md`。只读当前任务需要的主题。自定义主题生成不属于发布运行；即使用户同时提出创建主题，也必须先向 Leader 返回 `blocked`，把主题创建作为单独维护任务处理，禁止修改固定快照。
3. 主题优先级：用户明确指定 > 原 Skill 按文章题材自动推荐。流水线是非交互发布运行，未指定主题时直接采用原 Skill 的自动推荐并记录 `theme_source: auto`，不得由 Leader 预设风格。
4. 使用原 Skill 的组件装配和 `validate_gzh_html.py`，输出必须是纯 `<section>...</section>` 正文片段。

## 流水线内容策略

本次调用的用户约束是 `preserve-visible-text`，高于通用 Skill 的默认编辑性增强：

- 原文所有可见段落、代码、列表、表格和图片必须保留，不改写、不删减。
- 可以增加章节编号、目录标签和纯装饰性英文标签；可以用组件包裹关键词，但不得改变可见文字。
- 不自动新增作者介绍、互动 CTA、封面营销文案或原文没有的观点。
- 原文已有署名/CTA 时只排版原文内容，不再生成第二份。
- 不得留下 `{{...}}`、`图片URL`、`【插入...】` 等占位符；缺真实素材时返回 Leader，不得发布。
- 本地图片在 HTML `src` 中写可解析的绝对路径；直接引用 Designer 的原生图片，不复制或重命名。

## 产物与验收（Phase 2 优化：Worker 自验自修复，只返回最终结果）

固定写入：

```text
<run-dir>/article-body.html
<run-dir>/.pipeline/layout.json
<run-dir>/.pipeline/layout-validation.json
```

`layout.json` 使用 schema 1，包含：`protocol_version`、`run_id`、`mode: news`、canonical 目录、`article-source.md` 路径/hash、sealed 原稿路径/hash、gzh Skill 路径/hash、完整 tree SHA-256、实际读取文件、锁定的 upstream commit、主题决策、文章类型、`content_policy: preserve-visible-text`、标题/作者/摘要/封面元数据、HTML 路径/hash。

### 自验自修复循环（必须遵守）

HTML 和 layout.json 生成完成后，立即自验：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/validate_article_layout.py" \
  <run-dir>/article-body.html \
  --manifest <run-dir>/.pipeline/layout.json \
  --output <run-dir>/.pipeline/layout-validation.json
```

**验证失败处理流程**（最多重试 3 次）：
1. ERROR/WARNING 非零时，**自己读取 validation 报告，修复 HTML 或 layout.json**
2. 常见问题自动修复：
   - 图片路径错误 → 修正为 manifest 中记录的绝对路径
   - 占位符未替换 → 查找 `{{...}}` 并替换为真实内容
   - 标签结构破碎 → 重新生成对应 section
   - layout.json 字段缺失 → 补全 schema 要求的字段
3. 每次修复后重新运行验证
4. 3 次修复仍不通过，返回失败状态给 Leader

验证完全通过后再回报 Leader；不得调用 `run_context.py status`，由 Leader 复核并推进到 `layout_ready`。

### Phase 3 阶段重叠适配

如果 Designer 还在生成剩余图片（manifest 中存在 status = pending 的图片）：
- 只引用已经成功生成的图片（status = success）
- 未生成的图片在 HTML 中留占位：`<section class="pending-image" data-image-id="xxx"></section>`
- 记录 `layout.json.images.pending = [...]` 列出还在等待的图片 ID
- 所有图片完成后，可以通过 Leader 触发最终 HTML 补全

回报必须包含 `protocol_version`、`run_id`、主题、文章类型、HTML 路径/hash、图片数、校验结果、修复重试次数、canonical 目录和实际读取的 Skill/reference 文件。**验证完全通过后才返回 Leader**。
