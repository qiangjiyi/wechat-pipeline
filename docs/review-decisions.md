# Review 决策记录

## v2 Review（2026-07-13）

### 采纳并修复

- article 渲染临时目录泄漏：Node 失败时自行清理；成功后把目录所有权交给 Python，并由 `finally` 清理。
- dotenv 实现不一致：提取为 `shared/dotenv.py`，Doctor、图片后端预检和 Publisher 共用同一解析器。
- `seal` 缺少状态约束：只允许 `awaiting_input` 和 `input_sealed`。
- 非交互 `input()` 的 `EOFError`：统一转为带 `--yes` 提示的 `PublishError`。
- 固定 multipart boundary：改为每次请求随机生成，并清洗 multipart 文件名中的引号和换行。
- 临时聊天输入删除时机、formatter 跳过回报、plan 阶段 `output_sha256` 语义：补充为明确协议。
- CLI 版本输出首行假设、article subprocess 文本模式和 slug 错误上下文：按建议修复。
- Publisher 关键盲区：补充 token、正文图上传、草稿提交、source 分派、dotenv 和清理测试。

### 不采纳

- 引入模式注册器、`Cursor` 抽象：当前只有两个稳定模式和一个轻量解析器，抽象成本高于实际收益；新增第三个模式时再依据真实差异重构。
- 使用 PyYAML 或自动剥离行尾注释：Publisher 只声明支持平面 YAML；`#` 可能是合法值。文档明确边界，复杂输入使用 JSON。
- 收窄顶层 `OSError`：文件不存在、权限、磁盘等 IO 错误都应由 CLI 统一转成用户可读错误；保留异常文本，不隐藏原因。
- 重命名 `wechat-pipeline-init`：会破坏现有调用。README 已明确它是配置初始化，而非 run 初始化。
- 每个内置 Baoyu Skill 重复放置 License：Plugin 根目录已有 License、`THIRD_PARTY_NOTICES.md` 和固定快照 lock；复制文件会增加同步漂移。
- 用正文图片 URL 直接作为封面：微信文章封面要求永久素材的 `thumb_media_id`，正文 `uploadimg` 返回的 URL 不能替代。

### 已失效或属误判

- `get_material_url()` 未删除：当前代码中已不存在该函数。
- 命名账号回退全局凭据：当前 `account_value()` 仅在账号为 `default` 时读取全局值，命名账号不会回退。
- 硬编码 boundary 与文件名冲突是主要风险：真正风险是 boundary 与任意文件内容碰撞；随机化仍作为低成本稳健性改进采纳。

### 保留观察

- Leader 的 `~/.claude/agents/wechat-leader.md` 只用于本仓库软链接开发模式；正式 Plugin 安装使用 `${CLAUDE_PLUGIN_ROOT}`。在出现可复现的自定义 Agent 根目录需求前，不增加环境探测分支。
- Doctor 只检查配置是否存在，不在线验证 token 有效性，避免诊断命令产生外部副作用；真实有效性由发布请求返回。
