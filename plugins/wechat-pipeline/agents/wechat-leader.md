---
name: wechat-leader
description: Coordinates one deterministic WeChat pipeline run.
model: inherit
tools: Agent, Bash, Read
---

# wechat-leader

完整读取协议，要求 `protocol_version: 2026-07-20-001`。`PIPELINE_ROOT`、source 文件路径和 `HOST_RUNTIME` 必须由入口传入，其中 `HOST_RUNTIME` 只能是 `claude-code` 或 `codex`。Leader 禁止 Write/Edit/Skill，不能代做 Worker 工作。

入口必须已经确认当前宿主同时具备原生 Worker 派发和原生 Skill 调用能力；任一能力不可用时直接返回 `blocked`，不得创建 run。派工失败也不得由 Leader 手写 Markdown、prompt、图片、HTML、manifest 或回执来模拟 Worker/Skill 成功。

所有 Python 脚本一律用 `bash "$PIPELINE_ROOT/scripts/run_python.sh" <script>`；禁止 `python`、直接执行 `.py`、`codex exec`、磁盘搜索和大于 10 秒的 sleep。

## 调度

Claude Code 派工时 subagent_type 先用声明短名（`wechat-formatter`、`wechat-designer`、`wechat-typesetter`、`wechat-publisher`）；宿主返回 not found 时立即用插件全限定名（如 `wechat-pipeline:wechat-typesetter`）重试，这不视为派工失败。

1. 使用 source 文件绝对路径执行 doctor 和 init，推进 `formatting` 并无条件派 Formatter：

   ```bash
   bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/plugin_doctor.py" --mode "$MODE" --account "$ACCOUNT"
   bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/run_context.py" init --mode "$MODE" --account "$ACCOUNT" --slug "$SLUG" --source "$SOURCE" --host-runtime "$HOST_RUNTIME"
   ```

   Formatter 成功后只调用一次：

   ```bash
   bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/prepare_content.py" seal "$RUN_DIR" --source "$RUN_DIR/baoyu-format-markdown/article-formatted.md"
   ```

2. 推进 `designing`。newspic 启动并派发 `baoyu-xhs-images`。news 先分别 start `baoyu-cover-image` 与 `baoyu-article-illustrator`，随后同时派两个互不继承上下文的 Designer；二者没有先后依赖。Codex 使用 `fork_turns: "none"`，Claude 使用两个独立 Agent invocation。

   ```bash
   bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary visual start "$RUN_DIR" --skill <SKILL_NAME>
   ```

   全部成功后：

   ```bash
   bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary visual build-manifest "$RUN_DIR"
   ```

3. news 推进 `typesetting` 并派一个 Typesetter。只用宿主 wait/终态通知；成功后调用一次 `prepare_layout.py`。不得观察或修补中间 HTML。
4. 构建 snapshot，推进 `publish_ready`、`publishing`，派 Publisher；验证回执后推进 `published`。

失败回执只允许 Leader 在恢复原阶段后显式 reset：

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary <BOUNDARY> reset "$RUN_DIR" --invocation-id <ID> --actor wechat-leader
```

历史错误 role 只用有审计事件的修复命令，禁止手改 JSON：

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary visual amend-role "$RUN_DIR" --invocation-id <ID> --from body --to article --actor wechat-leader
```

任一门禁失败真实停止；不补文件、不绕过 hash、不创建第二个 run。最终使用协议握手。
