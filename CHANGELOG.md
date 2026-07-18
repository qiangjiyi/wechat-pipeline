# Changelog

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
