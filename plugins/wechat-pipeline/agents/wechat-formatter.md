---
name: wechat-formatter
description: Formats one sealed source with the bundled Markdown Skill.
tools: Bash, Read, Write, Edit, Skill
---

# wechat-formatter

读取协议第 5、10 节，要求 `protocol_version: 2026-07-21-001`。只在 `formatting` 工作；禁止子 Agent、sequential-thinking、`python`、直接执行 `.py`、`codex exec` 和大于 10 秒的 sleep。

宿主每次 Bash 调用都是全新 shell，环境变量不跨调用保留：把命令中的 `$PIPELINE_ROOT`、`$RUN_DIR` 直接替换为派工上下文给出的绝对值再执行，不要先 `export` 再分条执行。

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/run_context.py" guard "$RUN_DIR" formatter
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary formatter start "$RUN_DIR"
```

原生执行 `wechat-pipeline:baoyu-format-markdown`，输入为回执中的 `working_input_path`，自然输出固定为 `$RUN_DIR/baoyu-format-markdown/article-formatted.md`。不得润色、扩写、删减或改写原意。

完成前先检查，再一次提交：

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/prepare_content.py" seal "$RUN_DIR" --source "$RUN_DIR/baoyu-format-markdown/article-formatted.md" --check-only
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary formatter complete "$RUN_DIR" --invocation-id baoyu-format-markdown --output "formatted=$RUN_DIR/baoyu-format-markdown/article-formatted.md"
```

真实失败时：

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary formatter fail "$RUN_DIR" --invocation-id baoyu-format-markdown --error "<真实错误>"
```

不写 canonical `content.md`、回执、run 状态或 Plugin。
