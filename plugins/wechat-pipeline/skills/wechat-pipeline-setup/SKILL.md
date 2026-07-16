---
name: wechat-pipeline-setup
description: Initialize or diagnose the installed wechat-pipeline Plugin. Use when the user asks to configure, initialize, check, diagnose, or troubleshoot the WeChat publishing pipeline.
allowed-tools: Bash
argument-hint: "[--mode newspic|news] [--account ACCOUNT]"
---

# WeChat Pipeline Setup

Use the Plugin's own setup script; do not recreate configuration files by hand.

Resolve `PIPELINE_ROOT` first: use `${CLAUDE_PLUGIN_ROOT}` on Claude Code; on Codex, derive it from this Skill's absolute registry path (`<PIPELINE_ROOT>/skills/wechat-pipeline-setup/SKILL.md`).

1. Run `"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/plugin_doctor.py" --init`.
2. Tell the user which configuration file was created or already existed. Never read or print secret values.
3. Read trailing invocation arguments from the current user request (`$ARGUMENTS` on Claude Code). Accept only `--mode newspic|news` and `--account ACCOUNT`. If both are present, run:

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/scripts/plugin_doctor.py" $ARGUMENTS
```

4. If only one of `--mode` or `--account` is present, report the missing argument instead of guessing. With no arguments, initialization alone is complete.
5. Report missing configuration keys and runtime dependencies exactly as returned by the doctor. Do not edit the real `.env` file or request secrets in chat.
