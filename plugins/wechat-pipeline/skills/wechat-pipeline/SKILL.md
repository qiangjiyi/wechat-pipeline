---
name: wechat-pipeline
description: End-to-end WeChat Official Account draft pipeline. Use for 微信贴图、微信图文、公众号文章、草稿箱发布，以及需要从原始草稿完成 Markdown 格式化、图片生成、gzh-design 排版和微信草稿回读验证的完整流程。
---

# WeChat Pipeline

只做入口路由和 Leader 协调，不模拟 Formatter、Designer、Typesetter 或 Publisher。

## 启动

1. 解析 `PIPELINE_ROOT`：Claude Code 使用 `${CLAUDE_PLUGIN_ROOT}`；Codex 根据本文件绝对路径向上两级得到。
2. 完整读取 `<PIPELINE_ROOT>/docs/wechat-pipeline-protocol.md`，要求 `protocol_version: 2026-07-18-001`。
3. Claude Code 把用户原始请求交给 `wechat-pipeline:wechat-leader` 并停止外层执行。
4. Codex 当前 Agent 作为唯一 Leader；Use Codex subagent tools 派发所需 Worker。若工具不可用则返回 `blocked`，不能在 Leader 上下文代做。

## 唯一流程

1. 使用真实 source 初始化唯一 run，并执行 doctor。
2. 进入 `formatting`：必要时派 Formatter；调用 `prepare_content.py`；推进 `content_ready`。
3. 进入 `planning`：派 Designer 完成计划；门禁通过后推进 `rendering`，恢复同一 Designer 生图。
4. 图片门禁通过后推进 `artwork_ready`。
5. `news`：推进 `typesetting` 并派 Typesetter；门禁通过后推进 `layout_ready`。
6. 调用 `build_publish_snapshot.py`，推进 `publish_ready` 和 `publishing`。
7. 派 Publisher，只传 publish snapshot 和对应的 canonical 产物。
8. 验证 publish receipt，推进 `published` 并回报终态握手。

## 调度约束

- 一个 run、一个 canonical 目录、每个角色一个逻辑 Worker。
- 重试恢复同一 Worker，不创建第二个 run。
- Worker 只能写协议指定产物，不能修改 Plugin 源码或控制状态。
- V2 不允许 Designer 与 Typesetter 跨阶段并发。
- 只允许 Designer 内部对不同图片进行 batch 并行，默认上限 3；manifest 最后单次汇总。
- 不使用超过 10 秒的 sleep。
- 任一脚本门禁失败都真实报告，不补文件、不 placeholder、不直接调用下游 Skill。

## 原生 Skills

- Formatter：`wechat-pipeline:baoyu-format-markdown`
- Newspic：`wechat-pipeline:baoyu-xhs-images` + `wechat-pipeline:baoyu-image-gen`
- News：`wechat-pipeline:baoyu-cover-image` + `wechat-pipeline:baoyu-article-illustrator` + `wechat-pipeline:baoyu-image-gen`
- Typesetter：`wechat-pipeline:gzh-design`
- Publisher：`wechat-pipeline:wechat-publisher`

Worker 必须读取对应 Skill 当前原文和所需 references。协调器不能复述或重建 Skill 工作流。
