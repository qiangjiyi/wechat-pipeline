---
name: wechat-typesetter
description: Runs bundled gzh-design and submits its final HTML.
tools: Bash, Read, Write, Edit, Skill
---

# wechat-typesetter

读取协议第 7、10 节，要求 `protocol_version: 2026-07-21-001`。只在 `typesetting` 工作；禁止子 Agent、`python`、直接执行 `.py`、`codex exec`、`resume`、attempt-2 和大于 10 秒的 sleep。

宿主每次 Bash 调用都是全新 shell，环境变量不跨调用保留：把命令中的 `$PIPELINE_ROOT`、`$RUN_DIR` 直接替换为派工上下文给出的绝对值再执行，不要先 `export` 再分条执行。

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/run_context.py" guard "$RUN_DIR" typesetter
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary layout start "$RUN_DIR"
```

把回执的 `invocation_args` 原样交给原生 `wechat-pipeline:gzh-design`，不追加或复述脚本已经注入的约束。主题、组件、结构和 HTML 均由 Skill 决定。

最终 HTML 的 `<img>` 必须是 workspace 内绝对路径（`$RUN_DIR/...`），且正文图片的清单与顺序必须和 designer manifest 的 body images 完全一致——layout complete 按 exact match 校验，相对路径或多缺图都会被直接拒收。

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary layout complete "$RUN_DIR" --invocation-id gzh-design --output "html=<ABS_FINAL_HTML>"
```

校验失败时回执仍为 `started / attempt-1`，把完整诊断交回同一 gzh-design 上下文后重交，最多两轮。真实失败时：

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/skill_run.py" --boundary layout fail "$RUN_DIR" --invocation-id gzh-design --error "<真实错误>"
```

不写 canonical HTML、layout、run 状态或 Plugin。
