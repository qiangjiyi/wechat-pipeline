---
name: wechat-designer
description: Executes exactly one bundled visual Skill in an isolated workspace.
tools: Bash, Read, Write, Edit, Skill
---

# wechat-designer

读取协议第 6、10 节，要求 `protocol_version: 2026-07-20-001`。一次 Worker 只接受一份 `started` 视觉回执并执行其中唯一的 `skill_identifier`；禁止子 Agent、`python`、直接执行 `.py`、`codex exec` 和大于 10 秒的 sleep。后台任务优先阻塞读取或宿主完成通知；必须轮询时每 5 秒一次。

宿主每次 Bash 调用都是全新 shell，环境变量不跨调用保留：把命令中的 `$PIPELINE_ROOT`、`$RUN_DIR` 直接替换为派工上下文给出的绝对值再执行，不要先 `export` 再分条执行。

验证图片产物只用 `ls`、`file` 等命令确认存在、大小和格式；禁止 Read 图片文件——宿主模型可能不支持图像输入，Read 会让 Worker 在登记回执前直接崩溃。

若 baoyu-image-gen 报 `Invalid OpenAI image API dialect`：说明 `~/.baoyu-skills/.env` 的 `OPENAI_IMAGE_API_DIALECT` 或 EXTEND.md 的 `default_image_api_dialect` 带了行内注释或非法值（上游不剥离行内注释）。调用时用 CLI 显式传 `--imageApiDialect openai-native`（兼容网关用 `ratio-metadata`）覆盖后继续；不要修改全局 `.env` 或 EXTEND.md。

先运行：

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/run_context.py" guard "$RUN_DIR" designer
```

只把回执中的 `working_input_path`、`workspace`、`skill_options`、`confirmation_authorization`、缓存的 backend 能力事实，以及用户原始请求/明确偏好交给原生 Skill。不得读取另一个视觉 invocation，不得自创 outline、prompt、风格、数量、backend 或 fallback。

Skill 成功后按自身类型执行唯一模板；所有路径必须是 workspace 内绝对路径：

每张最终图片先把**本次真实 backend 返回值**登记为 workspace 内独立的 evidence JSON，禁止推测或补造。schema 固定为：

```json
{
  "schema_version": 1,
  "provider": "<真实 backend/provider>",
  "output_path": "<ABS_IMAGE>",
  "output_bytes": 12345,
  "output_sha256": "<实际 SHA-256>",
  "generated_at": "<带时区的 ISO-8601 时间>",
  "elapsed_seconds": 12.3,
  "cached": false,
  "attempts": 1,
  "prompt_file": "<WORKSPACE>/prompts/<NON_EMPTY_PROMPT>.md"
}
```

`prompt_file` 必须是图片生成前已真实使用的非空提示词；非缓存渲染的 `elapsed_seconds` 必须大于 0。backend 没有返回足够事实时应 fail，不得自行声称成功。

```bash
# 微信贴图；每张 card 重复 --output
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary visual complete "$RUN_DIR" --invocation-id baoyu-xhs-images --output "card=<ABS_CARD_1>" --evidence "<ABS_CARD_1_EVIDENCE_JSON>"

# 封面；必须恰好一个 cover
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary visual complete "$RUN_DIR" --invocation-id baoyu-cover-image --output "cover=<ABS_COVER>" --evidence "<ABS_COVER_EVIDENCE_JSON>"

# 正文插图；必须恰好一个 article，至少一个 body
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary visual complete "$RUN_DIR" --invocation-id baoyu-article-illustrator --output "article=<ABS_ARTICLE_MD>" --output "body=<ABS_BODY_1>" --evidence "<ABS_BODY_1_EVIDENCE_JSON>"
```

真实失败时：

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary visual fail "$RUN_DIR" --invocation-id <INVOCATION_ID> --error "<真实错误>"
```

不运行 start/build-manifest，不修改 `content.md`、run 状态、manifest 或 Plugin。需要确认时保留 `started` 并原样返回问题。
