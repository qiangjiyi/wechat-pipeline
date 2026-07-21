# Changelog

## 0.8.1

- 协议升级到 `2026-07-21-001`；Claude Code 与 Codex 统一由顶层 `wechat-pipeline` Skill 作为唯一 Leader，直接派发四类 Worker。
- 移除无法可靠二次派工的 `wechat-leader` 子 Agent，避免飞书/AAMP 入口形成“主线程 → Leader 子 Agent → Worker”嵌套调度并卡在 formatting。
- 入口 Skill 接管原 Leader 的确定性命令、Worker 命名兜底、并行隔离和失败恢复合同；新增结构测试阻止中间 Leader Agent 回归。

## 0.8.0

- 协议升级到 `2026-07-20-001`；三套 Skill 运行脚本合并为统一的 `skill_run.py --boundary formatter|visual|layout`，complete 合同、role 白名单、可复制命令示例和 workspace 边界完全显性化。
- run 初始化新增必填 `--host-runtime claude-code|codex`，声明与环境标记不一致时 fail closed；宿主缺少原生 Worker 派发或 Skill 调用能力时禁止创建 run，杜绝 Leader 静默退化为单 Agent 手搓流程。
- 视觉 complete 对每张 card/cover/body 强制校验 schema 1 执行证据：真实 provider、图片字节与哈希、生成时间、耗时、attempt、缓存状态和非空 prompt 必须互相一致；证据绑定进回执与 schema 5 manifest，并在 artwork gate 再次复核。
- Formatter 与 illustrator 在 complete 时执行源文保真门禁；失败回执支持 Leader 审计式 reset，历史 role 错误支持 `amend-role`，不再允许手改状态文件救火。
- 运行时与发布完整性合并到 `integrity.py`，恢复状态跳转门禁并加入元数据缓存与快照防篡改；stage guard、格式化验收、布局证据校验和公共 hash/JSON/Markdown 逻辑完成去重。
- News 封面与正文配图改为两个隔离 Worker 并行；backend 能力在 init 时缓存；Designer 轮询固定为 5 秒。
- 修复 baoyu-image-gen batch provider 优先级，显式 CLI provider 可覆盖 per-task provider；`codex-cli` 默认并发度提升为 2。
- 封面合同支持 PNG、JPEG、WebP 头部尺寸；补齐 reset、role、源文保真、provider 优先级与双格式封面回归测试。
- Doctor 新增 baoyu-image-gen dialect 预检：按上游 EXTEND.md → `~/.baoyu-skills/.env` 解析链校验，行内注释等非法值在创建 run 前报出具体文件与行号（规避上游不剥离行内注释的问题，vendored Skill 本体不改）。
- `init` 拒绝包含本地图片引用的源文（Obsidian `![[...]]` 嵌入、非 http 的 `![]()` 本地路径）并报出行号；正文图片只能来自 Designer manifest，避免本地截图变成占位文字进入已发布草稿。
- Worker 文档明确宿主 Bash 调用间环境变量不持久，命令中的 `$PIPELINE_ROOT`/`$RUN_DIR`/`$ACCOUNT` 需直接替换为派工绝对值；Designer 禁止 Read 图片文件并补充 dialect 排错指引；Typesetter 明确正文图片必须绝对路径且与 manifest exact match；Publisher 发布命令补 `--yes`；Leader 派工补插件全限定名兜底。
- layout workspace 去掉 `gzh-design/attempt-1/` 嵌套层，自然产物直接落在 `gzh-design/`（逻辑上的单 attempt 合同不变，只扁平化目录）。
- news 产物父目录由 `exports/wechat-articles/` 更名为 `exports/wechat-pipeline/`（run 复用按 exports 根目录全局扫描，旧目录中的历史 run 不受影响）。

## 0.6.7

- 修复 Designer 把“没有图片 API Key”误判为“没有任何生图后端”：调用记录现在携带不含密钥值的宿主能力事实，可识别已安装并登录的 `codex-cli`；具体 backend 与 fallback 仍由原生视觉 Skill 自主决定。
- 原生 Skill 声称无后端但宿主已有候选能力时，Designer 会把矛盾诊断交回同一个 Skill 进行一次真实渲染，不再直接生成错误失败回执。
- `baoyu-format-markdown` 改用顶层独立 workspace，自然产物保留在 `baoyu-format-markdown/`，未修改的执行期 `article.md` 在成功后自动清理。
- 协议升级到 `2026-07-18-007`。

## 0.6.6

- 文章配图调用显式附加“直接生成、不用确认、跳过确认、按默认出图”授权，原生 `baoyu-article-illustrator` 自动采用自身推荐配置继续执行，不再暂停询问视觉参数。
- 跳过确认不改变视觉所有权：preset、密度、风格、配色、图片数量和 prompt 仍完全由原生 Skill 自主决定。

## 0.6.5

- 每个视觉 Skill 使用全新隔离 Worker，原生 Skill 自主决定分析、prompt、风格、构图与 backend，不共享其他视觉上下文。
- Typesetter 在唯一 `attempt-1` 内把集成诊断交回同一个原生 `gzh-design` 上下文自我修正；Leader 只等待 Worker 终态和成功回执，不轮询中间文件。
- 移除产物目录的 `skill-output/` 包装层，直接展示 Skill 同名目录；成功后清理未修改的冗余 `article.md` 输入副本。
- 视觉 Skill 恢复按 `EXTEND.md/default_output_dir` 决定实际输出子目录，例如封面使用 `baoyu-cover-image/imgs/`。
- 初始化 slug 改为稳定语义名，并防御性移除末尾时间戳，避免目录名称重复日期和时间。

## 0.6.4

- 协议升级到 `2026-07-18-006`。
- Typesetter 改为只在隔离 workspace 中自然调用 `gzh-design`；Pipeline 只提供最终配图文章和“不新增作者/互动结尾”的发布场景约束，不干预主题、组件或内部工作流。
- 新增 `layout_skill_run.py`，记录准确的原生 Skill、输入、隔离 attempt 和最终干净 HTML；同一 Run 拒绝启动第二个 Typesetter。
- 新增 `prepare_layout.py`，由脚本确定性生成 canonical HTML、layout manifest、hash、lock 绑定和最终验收，Agent 不再手写或修补集成元数据。
- 最终门禁新增“不得生成关注、点赞、在看、转发、分享等结尾引导”检查，并把原生 gzh-design HTML 与发布快照绑定。
- Leader 在排版失败时只恢复同一 Typesetter 一次；禁止亲自编辑、提前派 Publisher、并发重跑或绕过 `layout_ready`。

## 0.6.3

- 协议升级到 `2026-07-18-005`。
- 完整 Pipeline 的 formatting 改为必经原生 Skill 阶段；不再根据 H1、标题数量或主观“结构合格”判断跳过 `baoyu-format-markdown`。
- `prepare_content.py` 只接受 `.pipeline/formatter-output.md`，明确拒绝把 sealed `.pipeline/input.md` 作为候选冒充 Formatter 产物。
- `format-result.json` 新增 Formatter 执行状态和原生 Skill 标识，`content_ready` 门禁同时验证固定路径与 hash。
- `--check-only` 只用于 Formatter 自然产物的保真自检，不再承担格式化必要性判断。

## 0.6.2

- 协议升级到 `2026-07-18-004`。
- `news` 封面比例提升为发布场景合同：控制面向原生 `baoyu-cover-image` 传递 `aspect: 2.35:1`，不再被全局 `EXTEND.md` 的通用默认比例覆盖。
- 原生 Skill 仍自主决定分析、prompt、风格、构图和图片 backend；Pipeline 只对最终封面执行 `2.35:1` 轻量门禁。
- 封面完成登记与 artwork manifest 验收都会拒绝错误比例，避免竖版图片进入排版和发布。

## 0.6.1

- 协议升级到 `2026-07-18-003`。
- 格式化阶段新增只读 `--check-only`，失败时返回缺失原稿的行号与片段预览。
- 文本保留校验忽略 Markdown、空白、全半角标点和引号样式，避免正常列表、引用和排版被误判为删文。
- 移除 Pipeline 固定 H2 数量，交由原生格式化 Skill 自然决定文章结构。
- Leader 不再修改 Formatter 产物或反复猜测修复；只允许恢复同一 Formatter 一次。

## 0.6.0

- 协议升级到 `2026-07-18-002`，将 `planning`/`rendering` 合并为单一 `designing` 阶段。
- Designer 只负责真实触发 `baoyu-xhs-images`、`baoyu-cover-image`、`baoyu-article-illustrator`；禁止手写 prompt、图片或 manifest 替代 Skill。
- 每个视觉 Skill 在独立 `skill-output/<invocation-id>/` 中完整执行自己的自然流程；Pipeline 不约束 outline、prompt、图片密度、风格、backend 或中间文件结构。
- 新增 `native_skill_run.py`，绑定准确的 `SKILL.md`、本次 content 输入、调用起止状态和 Skill 最终返回结果，并确定性生成 schema 4 manifest。
- News 排版直接消费文章配图 Skill 返回的最终配图文章，保留 Skill 的图片落位决定。
- 图片门禁聚焦原生 Skill 完成状态、最终结果绑定和发布可用性，不再校验 Skill 内部过程。

## 0.5.1

- 新增发布包 `release-integrity.json`，Doctor 与 run init 在冻结运行态前拒绝任何安装缓存篡改；重签名命令只存在于仓库维护层，不进入安装包。
- 固定 canonical 目录：用户产物为 `content.md`、`images/*.png`、`article-body.html`，过程文件统一进入 `.pipeline/`。
- Prompt 统一为 `.pipeline/prompts/*.md`；拒绝 YAML、空 prompt、旧 `prompts/`/`images/prompts/` 与未声明图片。
- planning gate 强制输出尚不存在、attempt 为空；render gate 将 prompt/output mtime 与真实调用时间窗绑定。
- 新增 Worker 精确阶段门禁；真实微信网络调用必须处于 `publishing`。
- Leader 必须通过宿主 Agent 声明派发并恢复同一 Worker，禁止探测不存在的 `agent.py` 或冒充 Worker。
- Doctor 强制通过 `run_python.sh` 调用且先于 init；失败时禁止 Agent 修改 Plugin 或环境自救。
- 长文格式化至少需要两个 H2，拒绝仅添加 H1 的伪格式化。

## 0.5.0

- 协议升级到 `2026-07-18-001`，按第一性原理重构为确定性控制面与 Agent 内容面。
- 正常 run 强制携带 source，新增 `content.md`、格式化保真门禁和运行时完整性快照。
- 状态转换自动执行门禁，`run.json` 增加 revision 与 checksum，拒绝直接状态编辑。
- 图片 manifest 增加强制模式契约、完整 PNG chunk/CRC/像素流、纯色空白图、最小尺寸/文件体积、真实 prompt 时间和 placeholder 拒绝。
- 移除提前排版及跨阶段并发，只保留 Designer 内部独立图片 batch 并行。
- 新增不可变 publish snapshot；Publisher 与最终回执强绑定 snapshot hash/fingerprint。
- HTML 封面和正文图必须与 designer manifest 精确一致，拒绝别名副本和未登记图片。
- 微信 `draft/get` 回读同时支持 `src` 与 `data-src` 图片。
- 新增 newspic/news 离线完整流程、状态篡改和伪造 backend 测试。

## 0.4.0

- 新增 `load_extend.py` 确定性解析 EXTEND.md（project / XDG / user-home 三级，外加 legacy `baoyu-imagine`），替代交给 LLM 不可靠的三级路径查找；Designer 直接读取解析结果。非交互模式下 EXTEND.md 未命中不再记为 `contract_error`，回退 Skill 内置默认值（`preferences.source=auto`，不记 `extend_path`）并继续生成。
- 流水线运行时降到约 10 分钟：Typesetter 与剩余图片生成并行、publish-ready 校验与 `gzh-design` 排版并发，Designer worker 自验自修复只返回最终结果。

## 0.3.0

- 协议升级到 `2026-07-13-001`：状态只由 Leader 写入，并新增只追加的运行事件日志。
- 新增 Python 3.10+ 自动解析启动器，避免系统 `python3` 版本过旧阻断可用运行时。
- Publisher 新增原子 `publish-result.json`、请求指纹、防重复恢复和 `draft/get` 回读验收。
- `published` 状态现在强制要求账号/模式匹配、`draft_media_id` 和成功的回读验证。
- Designer attempt backend 必须与 preflight 已配置 provider 一致，并规范化宿主 imagegen 命名。
- Publisher 输出分阶段上传、创建、验证进度，网络退避会显示重试次数和等待时间。
- 非幂等 `draft/add` 禁止自动重试；不确定结果持久化为安全阻塞回执，素材上传支持检查点恢复。
- Newspic Publisher 通过 manifest 强绑定 sealed 原文、图片顺序与 hash；回读验证禁止 `skipped` 冒充成功。
- 状态机按 newspic/news 分离，禁止跳过 rendering 或 news typesetting/layout；图片门禁增加尺寸、比例与重复产物检查。
- 原样纳入固定版本的 `gzh-design-skill`，新增独立公众号 HTML 排版阶段和校验门禁。
- Publisher 新增已排版 HTML 输入通道，保留现有正文图上传、封面素材和草稿 API 能力。
- 流水线协议升级，增加 typesetter、layout manifest 与可恢复的排版状态。
- 增加从本地已更新 Baoyu 仓库完整同步五个固定 Skill 快照的维护脚本。
- 默认输出目录对齐宿主全局工作区约定：`~/Workspace/exports`（原 `~/wechat-pipeline-exports`）。
- `plugin_doctor.py` 增加 exports 目录约定检查，未对齐时给出显式警告和修复指引。
- 新增 `shared.file_utils.is_relevant_file`，统一过滤 `.DS_Store`、`.pyc` 和 `__pycache__`，消除多脚本重复逻辑。

### Protocol 2026-07-11-002

- 将 publisher 网络退避从 Agent 提示词下沉到传输代码。
- 增加 run 初始化文件锁、运行身份校验和状态转换约束。
- 聊天输入改为先创建临时 source，再通过 hash-aware init 创建或复用运行。
- `PIPELINE_ROOT` 改为 Leader 派工必填字段，worker 不再独立扫描软链接。
- Codex 无结构化 Skill 附件时，必须传递准确 Skill 名和绝对 SKILL.md 路径。
- 已结构化 Markdown 不再空跑 formatter。
- Publisher Skill 拆为核心工作流和按需 references。
- 增加 publisher、article、proxy、run context、账号与输入解析测试。
- newspic 正文超过微信限制时明确失败，不再静默截断用户原文。
- 修复 article 渲染临时目录泄漏，并统一 Plugin 内的 dotenv 解析契约。
- 强化 seal 状态约束、非交互确认错误和随机 multipart boundary。
- 明确临时输入必须 finally 清理、formatter 跳过回报和 plan 阶段输出 hash 语义。
- 补充 token、正文图上传、草稿提交、输入解析和资源清理测试。

## 0.1.0

- 初始 Claude Code/Codex 双宿主 Plugin。
- 四角色流水线、唯一运行目录、Baoyu 固定快照和两阶段 manifest 校验。
