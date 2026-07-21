---
protocol_version: 2026-07-21-001
protocol_status: active
authority: single-source-of-truth
---

# 微信发布流水线协议 V2

本协议同时约束 Claude Code 与 Codex 的入口 Skill 及其 Worker Agent。目标是把一份原始草稿可靠地转换为一个经过微信草稿回读验证的产物。

## 1. 不可违反的原则

1. 每次请求只有一个 `run_id`、一个 canonical 目录和一个 Leader。
2. Agent 负责内容工作；确定性脚本负责状态、hash、门禁、幂等和发布快照。
3. Leader 不写 formatter、designer、typesetter 或 publisher 的产物。
4. Worker 只能写 canonical 目录，禁止修改 Plugin、Skill、validator 和 trust lock。
5. 校验失败只能修复同一阶段的真实产物，不能补造证据、placeholder、空白图或第二套目录。
6. Publisher 只能消费只读 `.pipeline/publish-snapshot.json`。
7. `draft/get` 回读验证是成功的必要条件；创建草稿不等于发布成功。

### Canonical 目录

```text
<run-dir>/
├── content.md
├── baoyu-format-markdown/             # 原生格式化 Skill 的独立自然产物
│   └── article-formatted.md
├── <skill-invocation-id>/             # 对应原生视觉 Skill 的独立自然产物
├── gzh-design/                        # 仅 news，原生排版自然产物
├── article-body.html                 # 仅 news
└── .pipeline/
    ├── input.md
    ├── backends.json                 # init 时一次探测并缓存
    ├── runtime-integrity.json
    ├── integrity-cache.json
    ├── formatter-skill-run.json
    ├── format-result.json
    ├── skill-runs/
    │   └── <skill-invocation-id>.json
    ├── run.json
    ├── events.jsonl
    ├── manifest.json
    ├── layout-skill-run.json         # 仅 news，gzh-design 调用边界
    ├── layout.json                   # 仅 news
    ├── layout-validation.json        # 仅 news
    ├── publish-snapshot.json
    └── publish-result.json
```

运行根目录下以 Skill invocation 命名的目录是各原生格式化、视觉和排版 Skill 的直接隔离工作区，不再增加 `skill-output/` 包装层。Skill 可以按自己的当前流程和 `EXTEND.md` 自由生成 analysis、outline、prompt、图片、HTML、预览页和修改后的文章；Pipeline 不规定这些中间产物的名称、结构或数量，也不把它们重新塑造成另一套格式。调用回执只记录输入边界、完成状态和 Skill 明确返回的最终结果。执行期只读 `article.md` 副本若未被修改、也未作为最终文章返回，会在 Skill 成功登记时删除。

## 2. 模式

- `newspic`：格式化原稿 → 由 `baoyu-xhs-images` 自主生成图片卡片 → 发布。
- `news`：格式化原稿 → 原生生成 `2.35:1` 封面和文章配图 → gzh-design 排版 → 发布。

用户明确模式优先。无显式信号时，长文或结构化 Markdown 使用 `news`，短内容使用 `newspic`。

## 3. 状态机

```text
input_sealed
  -> formatting
  -> content_ready
  -> designing
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
  --source <absolute-source-path> --host-runtime <claude-code|codex>
```

- `--host-runtime` 声明真正执行本次 run 的宿主运行时，写入 `run.json` 并纳入 state checksum；声明与环境标记矛盾时（声明 `claude-code` 却没有 `CLAUDECODE`，或声明 `codex` 却存在 `CLAUDECODE`）init 直接拒绝。入口必须先确认宿主具备派 Worker 子 Agent 和调用原生 Skill 的能力；纯聊天桥接等不具备该能力的宿主不得创建 run，直接回报 `blocked`。之后每次状态操作与 Worker guard 都复核该一致性，跨宿主接管 run 会被拒绝。
- 聊天正文先逐字写入权限 `0600` 的临时文件，并在 `finally` 删除。
- `--slug` 只表达稳定的 ASCII 语义，不包含日期、时间或随机串；目录唯一性由脚本生成的 `run_id` 提供。脚本会防御性移除 slug 末尾形如 `YYYYMMDD-HHMMSS` 的时间戳，避免与 run_id 重复。
- `.pipeline/input.md` 为权限 `0400` 的 sealed 原稿。
- 新建 run 前 `init` 拒绝包含本地图片引用的源文（Obsidian `![[...]]` 图片嵌入、非 http 的 `![](...)` 本地路径）；正文图片只能来自 Designer manifest，本地图片进入流水线只会变成占位文字。报出具体行号后由用户先处理源文，已存在的可复用 run 不受影响。
- 发布包自带只读 `release-integrity.json`，Doctor 和 init 会先验证安装态文件集合及 hash；安装态被修改时不得初始化 run。安装包只提供 validate，不提供重新生成 release manifest 的命令。
- `init` 同时写入 `.pipeline/runtime-integrity.json`，冻结当前 Agent、脚本、publisher、gzh-design、release manifest 和 trust lock 的 hash。
- 后续任一门禁先比较文件数、最新 mtime 和总大小；元数据变化时重算 hash，发布前与 published 门禁强制重算。发现运行时代码变化立即停止。
- `seal` 只用于旧版孤儿运行恢复，正常流程不得创建 `awaiting_input`。

创建 run 前必须精确运行：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/plugin_doctor.py" \
  --mode <newspic|news> --account <account>
```

禁止用系统 `python3` 绕过 `run_python.sh`。Doctor 未通过时不创建 run、不派 Worker，也不允许 Agent 修改 Plugin、Python、环境或 validator 来让检查通过。

Doctor 同时按 baoyu-image-gen 自身的解析链（EXTEND.md → `~/.baoyu-skills/.env`）预检 image dialect 配置；上游不剥离行内注释，非法值必须在此报出具体文件与行号，不允许留到视觉 Worker 内爆发。

## 5. 格式化

所有模式先进入 `formatting`。

- 输入边界：`.pipeline/input.md`；执行输入为 `baoyu-format-markdown/article.md` 临时副本。
- 每次完整 Pipeline 都必须原生执行一次 `baoyu-format-markdown`；原稿已有 Markdown 结构也不能跳过。Skill 可以自然决定不做或只做极少调整。
- Formatter 由 Skill 按内容自然决定标题层级、列表、引用、强调和必要 frontmatter，不改写原意。
- Formatter 通过统一 `skill_run.py --boundary formatter` 执行 start/complete/fail；complete 一次提交 `--invocation-id baoyu-format-markdown --output formatted=<绝对路径>`。
- Formatter 使用顶层独立 `baoyu-format-markdown/` workspace；最终自然产物固定为 `baoyu-format-markdown/article-formatted.md`，analysis 等原生自然中间产物保留在同一目录。成功登记后删除未修改的临时 `article.md`。
- Formatter 返回前必须调用 `prepare_content.py seal ... --check-only`。该模式只读校验并以 JSON 返回缺失片段的原稿行号和预览，不生成 canonical 产物。
- Leader 对 Formatter 自然产物调用：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/prepare_content.py" \
  seal <run-dir> --source <run-dir>/baoyu-format-markdown/article-formatted.md
```

脚本固定生成只读 `<run-dir>/content.md` 和 `.pipeline/format-result.json`，并冻结 Formatter 自然产物与 Skill 回执，验证：

- 恰好一个 H1；
- 原稿所有可见语义字符均保留；Markdown 标记、空白、全半角标点和引号样式不参与比较；
- Formatter 状态为 `executed`，Skill 标识为 `wechat-pipeline:baoyu-format-markdown`，顶层独立 workspace 和自然产物路径固定且 hash 一致；
- source、content 和 receipt hash 一致。

Pipeline 不规定 H2/H3 数量。`--check-only` 只能校验 Formatter 自然产物，禁止把 `.pipeline/input.md` 作为候选来决定是否跳过。门禁失败时 Leader 只能把完整诊断恢复给同一 Formatter，不得亲自修改 Markdown、改用原稿绕过或猜测不存在的状态参数。Formatter 自检最多两次；Leader 额外恢复最多一次，仍失败则保持 `formatting` 并返回 `blocked`。通过后才允许进入 `content_ready`。

## 6. 原生视觉 Skill 执行

视觉执行发生在唯一的 `designing` 阶段。Pipeline 只决定“此时调用哪个 Skill”并为其建立隔离边界，不决定“这个 Skill 怎样工作”。

### 调用并行关系

- newspic：完整调用一次 `wechat-pipeline:baoyu-xhs-images`。
- news：同时调用 `wechat-pipeline:baoyu-cover-image` 与 `wechat-pipeline:baoyu-article-illustrator`；两者只读同一 `content.md`，没有先后依赖。
- `baoyu-image-gen` 或其他图片 backend 是否被使用、怎样 batch、怎样 fallback，完全由上述原生 Skill 按自己的当前说明和 EXTEND 决定。

每次调用前，Leader 使用 `skill_run.py --boundary visual start` 创建 `<run-dir>/<invocation-id>/` 和执行期工作副本。init 已把 backend 探测缓存为 `.pipeline/backends.json`，visual start 只复用该事实。Skill 仍加载自身 `EXTEND.md`；news 封面的 `skill_options` 固定 `aspect: 2.35:1`。

每个 `started` 调用对应一个全新 Designer Worker。news 两个 Worker 并行运行；Claude Code 创建两个独立 Agent，Codex 对两者都使用 `fork_turns: "none"`。同一 invocation 只允许一个写者。

视觉 Worker 的派工只包含 start 返回的单次调用记录、用户原始请求和用户明确偏好。调用记录包含不含密钥值的 `image_backend_capabilities`，用于告诉原生 Skill 宿主侧实际可尝试的 backend（例如已安装并登录的 `codex-cli`）；它不是 backend 选择指令。Worker 通过运行时原生 Skill 机制执行记录中的精确 `skill_identifier`，并把工作副本、workspace、用户原始请求、明确偏好、`skill_options` 和能力事实原样交给 Skill。文章配图调用记录额外携带用户已授权的 `直接生成，不用确认，跳过确认，按默认出图。`，Worker 必须逐字附加到本次原生 Skill 请求，使其自动采用自己推荐的配置继续执行；该授权不允许 Pipeline 预选任何视觉参数。不得把排版发布任务、另一个视觉 Skill 的对话、analysis、outline、prompt 或自然产物带入本次原生 Skill 上下文。

如果原生 Skill 声称没有任何可用后端，而能力事实中的 `fallback_order` 非空，Worker 必须把矛盾诊断交回同一个原生 Skill 一次。原生 Skill 仍自主选择候选并执行真实渲染；只有真实 backend 调用失败才可以形成失败回执，不能再用“没有 API Key”替代对 Codex CLI 等候选能力的检查。

`skill_options` 只描述发布目的地要求，不接管 Skill 的内部工作流。不得再向 Skill 增加 Pipeline 自创的 outline、prompt、风格、图片数量、文件名或 backend 约束。分析深度、确认问题、图片数量、类型、风格、配色、构图、prompt、backend、batch、fallback 和中间产物均由原生 Skill 自主决定。Skill 需要确认时暂停并把问题交给用户；Skill 失败或不可用时停止，不允许 Worker 手工生成替代 prompt、图片或 manifest。

Skill 完整结束后，该单次视觉 Worker 只登记 Skill 明确返回的最终结果并立即结束：

- `baoyu-xhs-images`：最终 card 图片；
- `baoyu-cover-image`：最终选定的 cover 图片；
- `baoyu-article-illustrator`：最终 body 图片及其已经插入图片位置的最终文章。

中间是否存在 analysis、outline、reference、候选图片以及它们的格式，均属于 Skill 内部实现，不进入 Pipeline 合同。

每张返回的图片（card/cover/body）在 `complete` 时必须附带一份**执行证据** `--evidence <workspace 内绝对路径.json>`（schema_version 1），由 Worker 在生成完成后立即写入，字段包括：`provider`、`output_path`、`output_bytes`、`output_sha256`、`generated_at`（带时区）、`elapsed_seconds`（真实渲染必须 > 0，仅命中缓存可为 0）、`cached`、`attempts`、`prompt_file`。`prompt_file` 必须指向 workspace `prompts/` 内**非空**的该图提示词文件，且写入时间不晚于图片。complete 逐字段交叉校验证据与磁盘上的图片；证据缺失、prompt 为空、时间倒挂或字节/哈希不一致一律拒收——回执不再是 Worker 自报，placeholder 或无后端假图无法登记。证据路径与哈希随回执进入 manifest，`artwork_ready` 门禁复核不变。

所有目标 Skill 成功后，由 Leader 调用 `skill_run.py --boundary visual build-manifest` 确定性生成 schema 5 manifest。complete 在写回执前检查 role 基数、执行证据和原文一致性；illustrator 改写原文会当场拒收。视觉 Worker 不写 manifest。`artwork_ready` 门禁只验证：

- 模式要求的原生 Skill 是否各有一次成功完成记录；
- `skill_path` 是否精确指向当前插件内对应的 `SKILL.md`，名称和 hash 是否匹配；
- 每次调用是否绑定本次只读 `content.md`，开始/结束时间是否有效；
- 每次调用记录的 `skill_options` 是否与发布模式一致；
- 最终结果是否确实由对应 Skill run 返回，路径、hash 和文件是否仍然一致；
- newspic 是否收到 card 最终结果；news 是否收到选定 cover、body 以及原生配图 Skill 返回的最终文章，满足后续发布与排版的输入连接。
- news 最终封面是否满足 `2.35:1`，允许 3% 的图片后端尺寸取整误差。

门禁不检查 Skill 内部 prompt、outline、reference、风格、配色、图片数量、像素内容或 backend 决策；封面比例属于发布合同，不属于内部创作过程。通过后进入 `artwork_ready`。

## 7. 排版

仅 news 执行。Typesetter 必须等 `artwork_ready` 后启动；V2 不允许提前排版或 pending-image 占位。

- 输入只包含 designer manifest 中 `layout_input` 指向的原生配图 Skill 最终文章，以及一条发布场景约束：保留原文可见正文，不新增作者签名、作者介绍或关注、点赞、在看、转发、分享、三连、下篇见等结尾引导；文章在原文结束处自然结束。
- Typesetter 通过 `skill_run.py --boundary layout start` 取得唯一 `attempt-1`、workspace 和 `invocation_args`。
- Typesetter 通过 `skill_run.py --boundary layout complete --invocation-id gzh-design --output html=<绝对路径>` 提交最终 HTML。失败时保持同一 `started / attempt-1` 自我修正；脚本不提供 resume/attempt-2 活接口。
- Leader 派工后只等待宿主 Agent 的终态通知，不通过文件系统观察 HTML、workspace 或回执。Worker 终态后，Leader 只接受成功回执，再调用一次 `prepare_layout.py`，由脚本确定性复制最终 HTML、绑定原生 Skill 回执和 designer manifest、计算所有 path/hash/lock 元数据，并生成 `article-body.html`、`.pipeline/layout.json`、`.pipeline/layout-validation.json`。
- 最终验收只检查原生 Skill 是否完整成功、最终 HTML 合规、原文可见内容和图片是否保留、是否新增禁止的结尾引导、发布所需绑定是否一致。它不检查 Skill 内部 theme、组件选择、prompt、推理、辅助文件或中间步骤。
- ERROR 或 WARNING 任一非零都不能进入 `layout_ready`。校验修正只发生在同一 Typesetter、同一 `attempt-1` 和同一个原生 gzh-design 上下文中；禁止创建 `attempt-2`、并行启动第二个 Typesetter、Leader 手改或观察中间产物、提前派 Publisher或跳过门禁。

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

- 一个 run 的 Formatter、Typesetter、Publisher 各最多一个逻辑 Worker；每个视觉 Skill invocation 各有一个独立逻辑 Worker，禁止一个 Worker 执行多个视觉 Skill。
- 失败或等待用户确认后只恢复对应 invocation 的同一 Worker；每个视觉 Skill 都使用独立 Worker，彼此不复用上下文，也不能创建第二个 run。
- Worker 派工必须包含 protocol、`PIPELINE_ROOT`、run_id、canonical 目录、当前状态和允许写入的产物。
- Claude Code 与 Codex 都由入口主线程作为唯一 Leader，并通过宿主 Agent 工具直接派发 Worker。禁止派发中间 Leader Agent；`agents/*.md` 不是可执行 Python。禁止尝试 `agents/*/agent.py`，禁止 Leader 在派工失败后冒充 Worker。
- 每个 Worker 写入前必须执行 `run_context.py guard <run-dir> <worker>`，状态不精确匹配就停止。统一 Skill boundary 不重复做全树完整性扫描。
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
protocol_version: 2026-07-21-001
run_id: <run_id>
canonical_output_dir: <absolute path>
status: published | failed | blocked
owner: wechat-leader
next_action: report_to_user | resume_same_leader
direct_skill_fallback_allowed: false
```

协议升版必须同步 `protocol_version.py`、4 个 Worker Agent、2 个入口 Skill和本文件，共 8 处；结构测试负责看守。
