---
name: wechat-pipeline
description: End-to-end WeChat Official Account publishing coordinator. Use for 微信贴图, 微信图文, 公众号文章, newspic, news, formatting plus image generation plus gzh-design typesetting plus draft publishing, or when the user asks to run the complete WeChat publishing pipeline. On Claude Code it delegates exclusively to wechat-leader; on Codex it coordinates formatter, designer, typesetter, and publisher subagents.
---

# WeChat Publishing Pipeline

This Skill is the cross-host front door. It owns routing only; it must never imitate the formatter, image, typesetter, or publisher Skills.

## Resolve Host And Root

1. Check whether `CLAUDE_PLUGIN_ROOT` is present.
2. On Claude Code, pass the user's request verbatim to the `wechat-pipeline:wechat-leader` Agent and stop. Do not call any Baoyu or publisher Skill from this outer context.
3. On Codex, derive `PIPELINE_ROOT` from this Skill's absolute registry path: this file is `<PIPELINE_ROOT>/skills/wechat-pipeline/SKILL.md`.
4. Read `<PIPELINE_ROOT>/docs/wechat-pipeline-protocol.md` completely and require `protocol_version: 2026-07-13-001`.

## Codex Ownership

The user's invocation of this Skill explicitly authorizes the required Codex subagents. The current agent is the only Leader for the run.

- Use Codex subagent tools for formatter, designer, typesetter, and publisher workers.
- If subagent tools are unavailable, return `blocked`; do not perform worker tasks in the Leader context.
- Never launch a second run after failure. Resume the same worker and reuse the same `run_id` and canonical directory.
- Never invent prompts, images, manifests, validation evidence, or Skill-shaped artifacts.

## Initialize One Run

1. Resolve mode from explicit user intent first, then the protocol rules.
2. Resolve the account from the request or the sole configured account. If multiple accounts are configured and none was selected, ask only for the account.
3. For local file input, pass its absolute path to `run_context.py init --source`.
4. For chat input, preserve it byte-for-byte in a permission-`0600` temporary file, pass that file to `init --source`, and use `try/finally` to delete the temporary file whether initialization succeeds, fails, or is interrupted. Do not create an unhashed run and fill its input afterward.
5. Run `<PIPELINE_ROOT>/scripts/plugin_doctor.py` for the selected mode and account. Stop before dispatch if it fails.
6. Set the run status to `planning` with `--actor wechat-leader` before the first worker. The Leader is the only status writer; workers only return artifacts and evidence.

## Dispatch Workers

Every worker message must contain the protocol version, `run_id`, canonical output directory, sealed input path, mode, account, user-explicit visual parameters, and absolute `PIPELINE_ROOT`. Tell the worker to read the complete protocol before acting.

Dispatch sequentially:

- `newspic`: designer, then publisher.
- `news`: inspect the sealed input structure. If it already has usable Markdown headings/frontmatter, record that formatter was skipped; otherwise dispatch formatter. Run `prepare_article_source.py` on the formatter natural output, then dispatch designer with `article-source.md`, typesetter, and publisher in order.

Attach the exact installed Skills to each subagent as structured Skill inputs when the runtime supports it:

- Formatter: `wechat-pipeline:baoyu-format-markdown`.
- Newspic designer: `wechat-pipeline:baoyu-xhs-images` and `wechat-pipeline:baoyu-image-gen`.
- News designer: `wechat-pipeline:baoyu-cover-image`, `wechat-pipeline:baoyu-article-illustrator`, and `wechat-pipeline:baoyu-image-gen`.
- News typesetter: `wechat-pipeline:gzh-design`.
- Publisher: `wechat-pipeline:wechat-publisher`.

If structured Skill inputs are unavailable, include both the exact namespaced Skill name and its absolute `<PIPELINE_ROOT>/skills/<skill-name>/SKILL.md` path in the worker message. The worker must read that file and its referenced files directly. Do not replace it with a coordinator-authored summary.

The worker must execute the attached Skill's current `SKILL.md`, references, and selected `EXTEND.md` natively. It must not reconstruct the workflow from this coordinator summary.

`EXTEND.md` is resolved deterministically by the designer via `${PIPELINE_ROOT}/scripts/load_extend.py <skill> --base-dir <run-dir> --json`; the worker reads that absolute path directly instead of the skill's internal 3-tier lookup (which relies on shell parameter expansion and is unreliable when delegated to an LLM). When not found in non-interactive mode, the worker falls back to built-in defaults (`preferences.source=auto`, no `extend_path`) and must not trigger first-time setup, block, or return `contract_error`.

### Waiting Strategy (Phase 1 Optimization: Zero Long Sleep)

**NEVER use sleep commands longer than 10 seconds.** `sleep 30`, `sleep 60`, `sleep 120`, `sleep 180` are all FORBIDDEN.

After dispatching a worker, use only lightweight short-polling to wait for completion:
1. Check for target files every 5 seconds (`sleep 5 && ls` or `sleep 5 && test -f`)
2. Maximum 60 consecutive checks (5 minute total timeout)
3. Reset the timeout counter whenever progress is detected (new file, new directory, file size change)
4. Never "sleep for 2 minutes first then check"

If no progress for 12 consecutive checks, message the worker to ask for status. Do not keep waiting silently.

### Phase Overlap Strategy (Phase 3 Optimization: Typesetter Early Start)

For `news` mode, Typesetter does NOT need to wait for all images to complete.

Launch Typesetter when any of these conditions are met:
1. Cover image + at least 1 inline image completed successfully
2. OR >= 60% of total planned images are done
3. OR Designer has been running for 2 minutes with at least 1 successful image

After Typesetter is launched, **Designer can continue generating remaining images in the background** — they run in parallel.

Typesetter requirements for partial run:
1. Only reference successfully generated images (status = success in manifest)
2. Leave placeholder `<section class="pending-image" data-image-id="xxx"></section>` for pending images
3. Final HTML patch runs after all images complete

This changes "generate 4 images → typeset entire article" from serial to parallel, saving ~1.5-2 minutes.

### Phase 4 Optimization: Parallel Image Generation

When dispatching Designer for news article illustrations, explicitly pass `--batch-size 4` to enable parallel image generation. The `baoyu-article-illustrator` skill natively supports batching up to 4 concurrent images when the runtime supports parallel tool calls.

This changes 4 images from sequential (~1min × 4 = 4min) to parallel (~1min total), saving ~3 minutes.

### Phase 5 Optimization: Concurrent Validation and Typesetting

After **100% of images are complete**, launch both in parallel:
1. Run `--phase publish-ready` manifest validation
2. Set `typesetting` and dispatch Typesetter

Do **NOT** wait for manifest validation to complete before launching Typesetter. Both must pass before advancing to Publisher.

This saves the full manifest validation duration (~30 seconds) from the critical path.

### Validation Gates

After designer planning, run manifest validation with `--phase plan`, record a `validation.passed`/`validation.failed` event, set `rendering`, and resume the same Designer for generation. After all images complete, run `--phase publish-ready` **concurrently with typesetting**; do not block typesetting on manifest validation. After typesetting, run `validate_article_layout.py` with the layout manifest and set `layout_ready`. Both gates must pass before Publisher dispatch. A failed check goes back to the same worker; the Leader must not repair artifacts. Before Publisher dispatch set `publishing`; newspic must publish through `--manifest <run-dir>/.pipeline/manifest.json`. Require `.pipeline/publish-result.json` plus explicit `draft/get` verification, run `validate_publish_result.py <run-dir>`, and set `published` only after that gate passes. A `creation_status: unknown` receipt is a safety stop and must never trigger another `draft/add`.

## Finish

Report the account, mode, title, image count, `media_id`, and canonical directory. End with the exact `WECHAT_PIPELINE_RESULT` handshake defined by the protocol.
