# Changelog

## Unreleased

- 增加从本地已更新 Baoyu 仓库完整同步五个固定 Skill 快照的维护脚本。

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
