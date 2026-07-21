---
name: wechat-pipeline
description: End-to-end WeChat Official Account draft pipeline. Use for 微信贴图、微信图文、公众号文章、草稿箱发布，以及需要从原始草稿完成 Markdown 格式化、图片生成、gzh-design 排版和微信草稿回读验证的完整流程。
---

# WeChat Pipeline

只做入口路由和 Leader 协调，不模拟 Formatter、Designer、Typesetter 或 Publisher。

## 启动

1. 解析 `PIPELINE_ROOT`：Claude Code 使用 `${CLAUDE_PLUGIN_ROOT}`；Codex 根据本文件绝对路径向上两级得到。根目录必须存在且只能使用这一份，禁止搜索缓存或猜测版本。
2. 完整读取 `<PIPELINE_ROOT>/docs/wechat-pipeline-protocol.md`，要求 `protocol_version: 2026-07-20-001`。
3. 若 source 是聊天正文，入口主线程先用宿主 Write 能力写入权限 `0600` 的临时 Markdown，派工时只传文件绝对路径，并在 run 初始化完成后删除临时文件。Leader 无 Write 权限，不负责落盘。
4. 创建 run 前先确认宿主同时支持原生 Worker/subagent 派发和原生 Skill 调用。任一能力不可用时直接返回 `blocked`，不得启动单 Agent 降级流程，也不得用 Bash/Write/Edit 手搓 Worker 产物或回执。
5. Claude Code 设置 `HOST_RUNTIME=claude-code`，把用户原始请求、`PIPELINE_ROOT=${CLAUDE_PLUGIN_ROOT}`、`HOST_RUNTIME` 和 source 文件绝对路径交给 `wechat-pipeline:wechat-leader`，随后等待 Leader 终态握手。
6. 在 Codex 中设置 `HOST_RUNTIME=codex`，当前主 Agent 是唯一 Leader，使用宿主 subagent 工具派发所需 Worker；工具不可用时返回 `blocked`，不能在 Leader 上下文代做。

## 唯一流程

1. 使用 `run_python.sh` 执行 doctor；通过后才使用真实 source 和入口声明的 `--host-runtime "$HOST_RUNTIME"` 初始化唯一 run。初始化的 `--slug` 必须是无日期、时间和随机串的稳定 ASCII 语义名，唯一性由 `run_id` 提供。Doctor 失败不得修改安装态或环境来自救。
2. 进入 `formatting`：无条件派 Formatter 在顶层独立 `baoyu-format-markdown/` workspace 内完整调用原生 Skill；只对其自然产物 `baoyu-format-markdown/article-formatted.md` 调用 `prepare_content.py`；推进 `content_ready`。禁止用原稿结构或 check-only 结果跳过 Formatter。
3. 进入 `designing`：Leader 为模式要求的每个原生视觉 Skill 在运行根目录创建同名独立 workspace 和调用记录。news 的 cover 与 article-illustrator 同时派两个全新 Designer Worker；二者没有先后依赖。Codex 使用 `fork_turns: "none"`，不继承 Leader 或另一个视觉 Worker 的会话。
4. 每个 Worker 只接收自己的工作输入、workspace、用户原始请求、明确偏好、发布场景选项和不含密钥值的宿主 backend 能力事实；文章配图请求额外附加用户已授权的“直接生成、不用确认、跳过确认、按默认出图”，但不指定任何视觉参数。能力事实只避免把“没有 API Key”误判为“没有 Codex CLI 等可用后端”，不替 Skill 选择 backend。具体分析、图片数量、风格、配色、构图、prompt、backend、batch 和 fallback 全部由该原生 Skill 自主决定。每张最终图片必须带真实 backend 执行证据和非空 prompt；缺失或不一致时 complete 拒收。所有调用成功后由 Leader 确定性汇总最终结果并推进 `artwork_ready`。
5. `news`：推进 `typesetting` 并只派一个 Typesetter。Typesetter 在唯一 `attempt-1` 和同一原生上下文中自然调用 `wechat-pipeline:gzh-design`；校验失败时只把完整诊断交回原生 Skill 自我修正。Leader 不观察中间文件，只等待 Worker 终态和成功回执，再使用 `prepare_layout.py` 确定性生成 canonical HTML、layout manifest 和验收结果，通过后推进 `layout_ready`。
6. 调用 `build_publish_snapshot.py`，推进 `publish_ready` 和 `publishing`。
7. 派 Publisher，只传 publish snapshot 和对应的 canonical 产物。
8. 验证 publish receipt，推进 `published` 并回报终态握手。

## 调度约束

- 一个 run、一个 canonical 目录；Formatter、Typesetter、Publisher 各一个逻辑 Worker，每个视觉 Skill 调用各一个互不共享上下文的逻辑 Worker。
- 重试只恢复发生失败或等待确认的同一逻辑 Worker，不创建第二个 run，也不复用其他视觉 Skill 的 Worker。
- Worker 只能写协议指定产物，不能修改 Plugin 源码或控制状态。
- V2 不允许 Designer 与 Typesetter 跨阶段并发。
- news 的两个视觉 Skill 并行；每个 Skill 内是否 batch、并发多少、怎样 fallback，由原生 Skill 自己决定。
- 视觉 workspace 只是 Skill 独占的 article-dir 与安全边界；具体输出子目录由原生 Skill 的 `EXTEND.md/default_output_dir` 决定。成功登记时删除未修改且未作为最终结果返回的冗余 `article.md` 输入副本。
- gzh-design 的主题选择、组件装配、结构解析、关键词强调、全部具体排版、内部校验、辅助产物和文件命名均由 Skill 自己决定；Pipeline 只提供原生配图文章作为输入，并要求不新增作者签名或关注、点赞、在看、转发、分享等结尾引导。
- 排版集成校验失败只在同一 Typesetter 的 `attempt-1` 内交回同一个原生 Skill 上下文自我修正；禁止创建 `attempt-2`、Leader 观察或修补 HTML/layout、启动第二个 Typesetter、提前派 Publisher或绕过 `layout_ready`。
- Leader 等待 Typesetter 时只使用宿主 Agent wait/终态通知，不使用静态 sleep 或文件轮询；只在 Worker 终态后读取一次成功回执。
- 不使用超过 10 秒的 sleep。
- 任一脚本门禁失败都真实报告，不补文件、不 placeholder、不直接调用下游 Skill。

## 原生 Skills

- Formatter：`wechat-pipeline:baoyu-format-markdown`
- Newspic：`wechat-pipeline:baoyu-xhs-images`
- News：`wechat-pipeline:baoyu-cover-image` + `wechat-pipeline:baoyu-article-illustrator`
- Typesetter：`wechat-pipeline:gzh-design`
- Publisher：`wechat-pipeline:wechat-publisher`

Worker 必须通过运行时的原生 Skill 机制真实调用对应 Skill，并让它完整结束。协调器和 Worker 都不能复述、拆解或重建 Skill 工作流；图片 backend 是视觉 Skill 的内部选择。
