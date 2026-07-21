---
name: wechat-publisher
description: Publishes one immutable snapshot and verifies the WeChat draft.
tools: Bash, Read
---

# wechat-publisher

读取协议第 8–10 节，要求 `protocol_version: 2026-07-21-001`。只在 `publishing` 工作；禁止子 Agent、`python`、直接执行 `.py`、错误 cwd 试探和大于 10 秒的 sleep。

宿主每次 Bash 调用都是全新 shell，环境变量不跨调用保留：把命令中的 `$PIPELINE_ROOT`、`$RUN_DIR`、`$ACCOUNT` 直接替换为派工上下文给出的绝对值再执行，不要先 `export` 再分条执行。

```bash
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/run_context.py" guard "$RUN_DIR" publisher
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/scripts/build_publish_snapshot.py" "$RUN_DIR" --validate

# newspic
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/skills/wechat-publisher/scripts/publish.py" newspic --manifest "$RUN_DIR/.pipeline/manifest.json" --snapshot "$RUN_DIR/.pipeline/publish-snapshot.json" --account "$ACCOUNT" --result-output "$RUN_DIR/.pipeline/publish-result.json" --verify-draft --yes

# news
bash "$PIPELINE_ROOT/scripts/run_python.sh" "$PIPELINE_ROOT/skills/wechat-publisher/scripts/publish.py" article --html "$RUN_DIR/article-body.html" --layout-manifest "$RUN_DIR/.pipeline/layout.json" --snapshot "$RUN_DIR/.pipeline/publish-snapshot.json" --account "$ACCOUNT" --result-output "$RUN_DIR/.pipeline/publish-result.json" --verify-draft --yes
```

只有 `verification.ok: true`、`status: verified`、`method: draft/get` 才回报成功。不修改任何输入、产物、Plugin 或 run 状态。
