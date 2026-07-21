# wechat-pipeline 深度审查与改造方案（2026-07-19）

依据：对插件全部代码（5 agents / 20+ scripts / 2 SKILL.md / 协议文档 / 双 manifest / 3 个测试文件）的逐行审查，以及对 run `20260719-192213-498b5b` 真实执行会话（主会话 + leader + 5 个 Worker 子代理 transcript）的逐事件复盘。

---

## 第一部分：本次执行复盘

### 1.1 运行事实

- run：`20260719-192213-498b5b`，news 模式，账号 xiyue，19:22:13 → 19:57:16，全程 **35 分钟**，一次发布成功（终态门禁全过）。
- 执行入口：`Projects/labs/baoyu-design-lab` 的主会话 `d9ddc3a7`（只是个壳，读了一次协议后 spawn leader），实际协调在 leader 子代理 `agent-a7944c68`。
- token 消耗：全链 input 非缓存 ~1.29M，cache_read ~9.41M，output ~53.6k。leader（3.9M cache_read）和 typesetter（3.1M）占大头——主要来自救火和返工。

### 1.2 时间线分解（含浪费归因）

| 阶段 | 起止 | 净耗时 | 浪费 | 浪费内容 |
|---|---|---|---|---|
| leader 初始化 | 19:22:00–19:22:23 | 23s | ~0 | plugin_doctor + init，合理 |
| formatter | 19:22:23–19:24:22 | 2m00s | ~50s | `python` 不存在换 python3；ls 试探 ×3；H1 缺失返工；2 次 sequential-thinking |
| leader 交接 | 19:24:22–19:24:54 | 32s | ~10s | 重复跑 prepare_content（formatter 已验过同一份文件） |
| cover-image | 19:24:54–19:28:09 | 3m15s | ~1m05s | 瞎试 `codex exec`；complete 试错 4 次；实际出图仅 103s |
| illustrator | 19:28:32–19:46:33 | **18m01s** | ~3m20s | batch provider bug 全灭一轮；180s 固定 sleep 轮询；complete 试错 5 次并以**错误 role=body** 注册 |
| leader 救火 | 19:46:33–19:49:01 | 2m28s | **2m28s（全部）** | build-manifest 拒收 role=body，leader 14 步排查、手写 /tmp 脚本绕过校验改 JSON |
| typesetter | 19:49:09–19:53:44 | 4m35s | ~2m30s | 3 轮 complete 失败 + 12 次 Edit；根因：illustrator 把源文「别人」改成「他们」，校验以源文为准 |
| publisher | 19:54:15–19:57:11 | 2m56s | ~37s | cwd 错误 + find/--help 试探；发布本身 2m09s 一次成功，全场最干净 |

### 1.3 三个系统性根因

1. **complete 契约对 agent 不可见。** `native_skill_run.py / layout_skill_run.py / formatter_skill_run.py` 的 complete CLI 契约（`role=/abs/path`、输出必须在 workspace 内、`--invocation-id`）没有写进任何 agent 可见的说明，错误消息也不给示例。三个 agent 共 **12 次试错**重新发现同一契约；illustrator 最终静默注册成 role=body，代价放大成 leader 的 2.5 分钟救火——而 leader 是用"绕过校验手改状态文件"修复的，破坏了确定性门禁的设计意图。
2. **阶段间缺"源文一致性"前置校验。** illustrator 违反"不重写原文"约束改了一个字，代价由 typesetter 付（3 轮 complete 失败 + 12 次 Edit，2.2 分钟）。该检查本可在 illustrator complete 时确定性完成（diff 源文段落）。
3. **视觉阶段物理串行。** codex-cli 后端 concurrency=1，5 张插图串行 13.8 分钟；cover（103s 实际出图）本可与插图并行，协议却强制"前一个成功才能开始下一个"。图片生成是总时长的物理下限。

---

## 第二部分：插件问题清单

### A. 冗余

| # | 严重度 | 问题 | 证据 | 状态 |
|---|---|---|---|---|
| A1 [✅已落地] | 高 | `sha256_file` 逐字复制 12 处；`write_json`/`load_json`/`inside`/`now_iso`/`tree_sha256` 各复制 3–8 处；`shared/` 形同虚设 | run_context.py:66、native_skill_run.py:41 等 12 处 | 已建立 `shared/hashing.py` + `shared/jsonio.py`，所有脚本 import 复用 |
| A2 [✅已落地] | 高 | `EXPECTED_SKILLS`/`ALLOWED_ROLES` 双份硬编码，新增 skill 要同步两处 | native_skill_run.py:29-37 vs validate_designer_manifest.py:20-28 | `skill_run.py` 单源定义，`validate_designer_manifest.py` import 复用 |
| A3 [✅已落地] | 中 | 三个独立 frontmatter/title 解析器，剥引号规则各异 | prepare_layout.py:54-65、mode_newspic.py:43-58、source_loader.py:52-82 | 统一到 `shared/markdown_meta.py`（split_frontmatter/frontmatter/title/first_h1/markdown_body） |
| A4 [✅已落地] | 中 | validate_article_layout.py 内部图片提取/段落校验逻辑自我复制两遍 | 182-197 vs 392-406；163-180 vs 448-467 | `_extract_local_images()` 抽取为单一函数，两处调用复用 |
| A5 [✅已落地] | 中 | leader 提示词里的手工回执核对清单与 prepare_layout.py:68-105 的确定性校验重复，且脚本更严 | wechat-leader.md:30 | leader.md 已精简为纯脚本调度，无手工核对清单 |
| A6 [✅已落地] | 低 | 占位符/标签正则双份 | validate_article_layout.py:30-35 vs html_article.py:22-27 | 正则统一到 `shared/html_contracts.py:PLACEHOLDER_PATTERNS`，import 复用 |
| A7 [✅已落地] | 低 | 同一规则（禁 attempt-2、禁互动引导）在 4–5 个文件复述；脚本已注入的内容仍在教 LLM | leader.md:43-44、typesetter.md:12,17、SKILL.md:36-37、protocol.md:178-183 | agent .md 已精简，脚本注入的规则不再在提示词中复述 |

### B. Bug 与正确性

| # | 严重度 | 问题 | 证据 | 状态 |
|---|---|---|---|---|
| B1 [✅已落地] | **高** | **状态跳转的运行时完整性门禁被注释掉**，算完即弃；协议 :96 仍承诺"代码变化立即停止"；结构测试看守的是错误的文件（断言 build_publish_snapshot.py，注释实际在 run_context.py） | run_context.py:371-374；test_plugin_structure.py:127-132 | `validate_transition_gate()` 已激活 `validate_runtime()` 调用，测试断言对象已改为 run_context.py |
| B2 [✅已落地] | **高** | failed 恢复语义三方打架：状态机允许 failed→failed_from，但 native/formatter skill_run 的 start 对 failed 回执直接 raise（无 resume）；layout 有 resume 子命令却被协议禁止。**视觉/格式化失败实际不可恢复** | run_context.py:337-339、native_skill_run.py:110-115、layout_skill_run.py:141-152 | 三边界统一 `reset` 子命令（Leader 专用），删除 `resume`；start 对 failed 回执提示 reset 命令 |
| B3 [✅已落地] | 中 | 封面尺寸合同只解析 PNG，JPEG/WebP 封面比例正确也被拒，错误信息误导 | image_contracts.py:22-31 | `image_dimensions()` 支持 PNG/JPEG/WebP 三种格式，含 `_jpeg_dimensions()` 和 `_webp_dimensions()` |
| B4 [✅已落地] | 中 | proxy_client errcode 二次检查永不可达（死代码） | proxy_client.py:160-161、221-222 vs :64-65 | errcode 检查仅保留 L64-65 一处，死代码已删除 |
| B5 [✅已落地] | 中 | start 复用 started 回执时不复核 workspace 现场 hash | native_skill_run.py:110-114 | `validate_reusable_record()` 全量复核 input hash、skill hash、workspace、output hash |
| B6 [✅已落地] | 低 | lock 文件读取无 try 保护 | validate_article_layout.py:313 | lock 读取已加 `try/except (OSError, json.JSONDecodeError)` 保护 |
| B7 [✅已落地] | 低 | seal_run 不持 run_lock | run_context.py:295-319 | `seal_run()` 在 `with run_lock(run_dir):` 上下文中执行 |
| B8 [✅已落地] | 低 | record_event 不校验 actor 白名单 | run_context.py:402-415 | `append_event()` 校验 `actor not in ALLOWED_ACTORS` |

### C. 歧义与不一致

| # | 严重度 | 问题 | 证据 | 状态 |
|---|---|---|---|---|
| C1 [✅已落地] | 高 | leader 被 `disallowedTools: Skill, Edit, Write` 禁止 Write，却被指示"用宿主 Write 能力写 0600 临时文件"；入口 SKILL.md 启动流程只字未提这一步 | wechat-leader.md:5,17 vs skills/wechat-pipeline/SKILL.md:11-15 | SKILL.md step 3 明确 0600 临时文件流程（入口主线程落盘），leader.md 改为 source 必须是文件路径 |
| C2 [✅已落地] | 中 | `--phase` 参数纯摆设，函数体从未引用 | validate_designer_manifest.py:302 | `--phase` 参数已从 argparse 中删除 |
| C3 [✅已落地] | 中 | layout_skill_run 暴露 resume（attempt-2）活接口，协议却禁止 attempt-2——"脚本不拦、只靠 LLM 自觉"，与确定性 gate 哲学相反 | layout_skill_run.py:141-152 vs protocol.md:180,183 | `skill_run.py` 无 `resume` 子命令，协议和 typesetter.md 明确禁止 |
| C4 [✅已落地] | 中 | 协议版本号 9 处硬编码（有测试护栏，可接受但需注明升版流程） | protocol_version.py:3 + 5 agents + 2 SKILL.md + protocol.md | protocol.md:L262 已注明"协议升版必须同步 9 处；结构测试负责看守" |
| C5 [✅已落地] | 低 | 授权文案三处逐字复制 | native_skill_run.py:38、designer.md:33、test:1201 | 授权文案定义为 `skill_run.py` 常量 `ARTICLE_ILLUSTRATOR_CONFIRMATION_AUTHORIZATION`，agent .md 引用字段名 |
| C6 [✅已落地] | 低 | "Codex 当前 Agent 作为唯一 Leader；Use Codex subagent tools 派发所需 Worker" 病句且被测试当锚点 | skills/wechat-pipeline/SKILL.md:15 | 已改为"在 Codex 中，当前主 Agent 是唯一 Leader，使用宿主 subagent 工具派发所需 Worker" |
| C7 [✅已落地] | 低 | plugin_doctor 把本机私有约定 `~/Workspace/exports` 硬编码为对所有用户的警告 | plugin_doctor.py:148-157 | exports 路径信息已从 warnings 降级为 info |

### D. skill/plugin creator 最佳实践

| # | 严重度 | 问题 | 状态 |
|---|---|---|---|
| D1 [✅已落地] | 中 | 5 个 agent 只有 `disallowedTools` 黑名单、无 `tools:` 白名单，Formatter/Designer/Typesetter 实际持有 WebFetch/WebSearch/全部 MCP | 所有 agent 已改用 `tools:` 白名单，删除 `disallowedTools` |
| D2 [✅已落地] | 低 | `background: false` 是非标准 frontmatter 键，被静默忽略，测试还在为它站岗 | 已删除 `background` 键及对应测试断言 |
| D3 [✅已落地] | 低 | 每个 Worker 都被要求"完整读取协议"（257 行），其实各自只用其中一节 | 每个 Worker 只读相关章节（formatter: 5/10、designer: 6/10、typesetter: 7/10、publisher: 8-10） |
| D4 [✅已落地] | — | 做得好的：入口 SKILL.md 51 行含中文触发词；wechat-publisher SKILL.md 按需加载 references；判定大量下沉脚本——方向正确 | 维持原状，方向正确 |

### E. 性能/耗时

| # | 严重度 | 问题 | 证据 | 状态 |
|---|---|---|---|---|
| E1 [✅已落地] | 高 | 241 文件全树 hash 每次 run 算 15–20 遍，其中 ~6 遍算完即弃（B1）；stage_guard 与 *_skill_run 的 require_* 几秒内重复跑 | runtime_integrity.py、run_context.py:371-374、stage_guard.py:35 | `integrity.py:validate_runtime()` 按 (file_count + latest_mtime_ns + total_size) 做 memo，每 run 全树 hash ≤3 次（测试断言 `full_hash_count == 3`） |
| E2 [✅已落地] | 中 | plugin_doctor 三重完整性 hash（release 241 文件 + gzh tree + 每个 baoyu skill tree），后两组在集合上被第一组完全覆盖 | plugin_doctor.py:123,143,146 | plugin_doctor 只调 `validate_release()`，baoyu/gzh tree 校验移交 CI |
| E3 [✅已落地] | 中 | 每次视觉 skill start 都同步探测 codex CLI（最多 4 次子进程、理论最坏 40s stall） | native_skill_run.py:142、preflight_image_backends.py:58,63 | `cached_backends()` 缓存到 `.pipeline/backends.json`，init 时探测一次，后续复用 |
| E4 [✅已落地] | 中 | layout 全量校验链重复 5 次（complete→prepare_layout→门禁→snapshot→publisher），中间三次在几秒窗口内对相同只读产物重算 | layout_skill_run.py:180-185、prepare_layout.py:176、run_context.py:384-386、build_publish_snapshot.py:80 | `validate_snapshot_evidence()` 轻量校验（不重算 hash），全量校验只在 publishing + published |
| E5 [✅已落地] | 中 | news 两个视觉 skill 强制串行，但脚本层并无依赖——纯提示词约束 | wechat-leader.md:23、protocol.md:146 | leader.md + SKILL.md + protocol.md 已改为 cover 与 illustrator 同时派发 |
| E6 [✅已落地] | 低 | snapshot 校验链重复 4 次 | run_context.py:388-391、publisher.md:12、pipeline_snapshot.py:25-31、validate_publish_result.py:68-71 | `pipeline_snapshot.py` 委托 `build_publish_snapshot.py --validate`，校验链分层（轻量/全量/最终） |

### F. 执行会话暴露的额外问题（代码审查之外）

| # | 严重度 | 问题 | 状态 |
|---|---|---|---|
| F1 [✅已落地] | 高 | baoyu-image-gen 的 `build-batch.ts` 生成 batch.json 默认 provider=replicate，`main.ts --provider codex-cli` 不覆盖 batchfile per-task provider → 5 张图全部 `REPLICATE_API_TOKEN is required` 秒败一轮 | `build-batch.ts` 不再写死 replicate（默认省略 provider），`main.ts:createTaskArgs()` CLI --provider 优先级最高，覆盖 per-task provider |
| F2 [✅已落地] | 高 | complete CLI 契约对 agent 不可见（详见 1.3），12 次试错 + role 注册错误 + leader 绕过校验救火 | `contract_error()` 附可复制命令示例，`validate_role_cardinality()` 前置 role 检查，`amend_role` 审计修复命令，每个 Worker .md 有完整命令模板 |
| F3 [✅已落地] | 中 | 无"源文一致性"确定性校验：illustrator 改写原文一个字，代价由 typesetter 付 2.2 分钟 | `validate_preservation()` 在 formatter 和 illustrator complete 时执行源文逐段比对 |
| F4 [✅已落地] | 中 | agent 侧环境试探成为固定税：`python` vs `python3`、直接执行 .py（exit 126）、错误 cwd、瞎试 `codex exec`、EXTEND.md 路径两次 ls——全场 6+ 处实例 | 所有 agent .md 顶部加铁律：一律 `bash run_python.sh`，禁止 python/.py/codex exec；每个 agent 有完整命令模板 |
| F5 [✅已落地] | 中 | illustrator 用固定 sleep 60/30/30/60 轮询后台任务 + 先空等 80s，第 5 张图早已产出 | designer.md 规定阻塞读取优先，轮询间隔 ≤5s |
| F6 [✅已落地] | 低 | formatter 的 2 次 sequential-thinking（~13s）价值存疑；leader 重复 prepare_content | formatter.md 禁止 sequential-thinking；leader.md 无重复 prepare_content |

---

## 第三部分：深度改造方案

### 总目标

- **墙钟：35min → ≤18min**（不保真压缩则 → ~15min）。
- **token：-40% 以上**（消灭救火与返工，leader cache_read 3.9M → <1.5M）。
- **零试错**：所有 agent 一次跑通 complete，不允许出现"试错发现契约"。
- **协议与脚本语义严格对齐**：脚本不允许的提示词不许承诺，提示词禁止的脚本不提供活接口。

### Phase 0 — 止血（改动小、收益大，先行）

1. **修 B1（门禁根基）[✅已落地]**：已恢复 `run_context.py:validate_transition_gate()` 的 `validate_runtime()` 调用，失败时 `raise SystemExit`。`test_plugin_structure.py` 的断言对象已扩到 run_context.py，断言 `raise SystemExit("runtime integrity gate failed:` 存在。
2. **complete 契约显性化（F2）[✅已落地]**：
   - `skill_run.py` 的 `contract_error()` 在每条校验错误消息末尾附上可直接复制的正确命令示例（含 role 表、workspace 约束、`--invocation-id`）。
   - complete 校验前置 role 白名单检查：`validate_role_cardinality()` 在 complete 时立即检查 role 基数。
   - 给 leader 一个正经修复命令：`skill_run.py amend-role --run-id X --invocation-id Y --from body --to article`（重算 sha、留事件）。
   - 每个 Worker agent md 里贴出它自己那次 complete 的完整命令模板（一行，带占位符）。
3. **源文一致性前置校验（F3）[✅已落地]**：在 `skill_run.py:complete()` 的校验链里加入 `validate_preservation()`——formatter 和 illustrator complete 时逐段归一化比对源文。逻辑复用 `shared/text_preservation.py:preservation_report()`。
4. **环境试探税清零（F4）[✅已落地]**：
   - 所有 agent md 顶部加铁律：一律 `bash scripts/run_python.sh <script>`；禁止 `python`、直接 `./x.py`、`codex exec`。
   - publisher agent md 直接给出 publish.py 的完整命令模板；typesetter/designer 同理给 complete 模板。
5. **F1 provider bug [✅已落地]**：`build-batch.ts` 的 `--provider` 改为可选（默认省略，不写死 replicate）。`main.ts:createTaskArgs()` 中 CLI `--provider` 优先级最高（`providerSource === "cli" ? baseArgs.provider : task.provider ?? baseArgs.provider ?? null`），覆盖 batch file per-task provider。
6. **F5 轮询 [✅已落地]**：designer agent md 规定后台长任务用"阻塞读取/宿主完成通知"模式，禁止 `sleep 60` 轮询；需要轮询时间隔 ≤5s。
7. **E3 探测缓存 [✅已落地]**：`probe_image_backends()` 结果每个 run 缓存进 `.pipeline/backends.json`（init 时探测一次），后续 start 直接复用。`skill_run.py:cached_backends()` 实现缓存查询。
8. **leader 去重 [✅已落地]**：删掉 leader 在 formatter 完成后的重复 prepare_content 全量校验；提示词里的手工核对清单全删，判定全交脚本；formatter 禁掉 sequential-thinking。

**Phase 0 预计可省：~9–10 分钟（救火 2.5 + typesetter 返工 2.2 + 试错 1.5 + 试探 1.5 + 轮询 1.5 + 重复校验 0.5），35min → ~25min。**

### Phase 1 — 结构合并与检查去重

1. **shared 公共库（A1/A3）[✅已落地]**：已新建 `shared/hashing.py`（sha256_file/tree_sha256/hash_file_set）和 `shared/jsonio.py`（load_json/write_json 原子写/inside/now_iso），所有脚本 import 复用；frontmatter/title 解析统一进 `shared/markdown_meta.py`。
2. **合并三个 `*_skill_run.py` → `scripts/skill_run.py --boundary formatter|visual|layout` [✅已落地]**：回执 schema、start/complete/fail 骨架、工作副本生命周期统一。合并时统一了：
   - failed 恢复语义（B2）：三边界统一支持 leader 显式 `reset`；删除 layout 的 `resume`（C3）。
   - start 复用回执时复核 workspace 现场 hash（B5）：`validate_reusable_record()` 全量复核。
   - role 白名单单源（A2）：`ALLOWED_ROLES`/`EXPECTED_SKILLS` 定义在 `skill_run.py`，`validate_designer_manifest.py` import。
3. **合并 `runtime_integrity.py` + `release_integrity.py` → `scripts/integrity.py` [✅已落地]**：capture/validate --scope release|runtime，落实门禁决策。
4. **`validate_formatted_content.py` 并入 `prepare_content.py` [✅已落地]**：`validate_content_artifact()` + `validate_candidate()` + `seal()` 子命令（含 `--check-only`）。
5. **`stage_guard.py` 并入 `run_context.py guard` 子命令 [✅已落地]**：`validate_worker_stage()` + `guard_worker()` 子命令。
6. **完整性 hash 缓存（E1/E2）[✅已落地]**：同一 run 内按（file_count + latest_mtime_ns + total_size）做 memo；plugin_doctor 只跑 release validate，baoyu/gzh tree 校验移交 CI。
7. **layout 校验链去重（E4/E6）[✅已落地]**：layout_ready 门禁与 publish_ready 用轻量校验（`validate_snapshot_evidence` 只验指纹）；全量校验只保留 publishing 一次 + published 一次。`pipeline_snapshot.py` 委托 `build_publish_snapshot.py --validate`。
8. **小修 [✅已落地]**：B3（JPEG/WebP 头解析）、B4（删死代码）、B6（try 保护）、B7（seal 进锁）、B8（actor 白名单）、C2（删 `--phase`）、C7（doctor 私有路径降级为 info）、A4（抽 `_extract_local_images`）、A6（正则单源到 `shared/html_contracts.py`）。

**Phase 1 预计可省：~1–2 分钟墙钟（hash/校验去重）+ 脚本总数 20+ → 14，维护成本显著下降。**

### Phase 2 — 协议与提示词瘦身

1. **C1 归属修正 [✅已落地]**："聊天正文先落 0600 临时文件再 init"写进入口 `skills/wechat-pipeline/SKILL.md` 启动步骤（主线程才有 Write）；leader.md 改为"source 必须已是文件路径"。
2. **Worker 提示词减肥（A7/C5/D3）[✅已落地]**：协议文档保留全量；每个 Worker md 只写：① 必读协议第 N 节 ② 本 Worker 的输入/输出合同 ③ 完整命令模板。删除所有脚本已注入/校验的规则复述。
3. **工具白名单（D1/D2）[✅已落地]**：formatter/designer/typesetter 用 `tools:` 白名单收窄到 Bash/Read/Write/Edit(+Skill)；publisher 收窄到 Bash/Read。删除 `background: false` 及对应测试断言。
4. **C6 病句修复 [✅已落地]** 并同步测试锚点。
5. **协议版本号（C4）[✅已落地]**：维持 protocol_version.py 单源 + 测试护栏，protocol.md 加一行"升版本必须同步 9 处"。

### Phase 3 — 并行化与调度（墙钟大头）

1. **E5 视觉 skill 并行 [✅已落地]**：已修改 wechat-leader.md 与 protocol.md——news 模式下 cover-image 与 article-illustrator 同时派两个 Worker。脚本层本就独立，无需改代码。
2. **插图并发生成 [⚠️部分落地]**：codex-cli 后端 concurrency 从 1 调到 2（`main.ts:DEFAULT_PROVIDER_RATE_LIMITS`），但未达到建议的 2-3 上限。provider 级并行（不同 task 分不同 provider）未实现。

   **剩余实施方案**：
   - 将 `main.ts` 中 `"codex-cli": { concurrency: 2 }` 调到 `3`（若 codex 允许）
   - 在 `build-batch.ts` 中支持 per-task provider 分配策略（如轮询分配不同 provider），实现 provider 级并行
   - 预计可再省 ~3-4 分钟（5 张图从 ~7min → ~5min）

3. **typesetter 提前预热（可选）[❌未落地]**：gzh-design 的主题选择不依赖插图，typesetter 可在 designing 阶段并行完成主题决策与骨架 HTML，artwork_ready 后只填图。原计划标注"二期候选"。

   **实施方案（如需推进）**：
   1. 修改协议允许"骨架先行、图片后填"模式
   2. 在 `run_context.py` 中新增 `designing_with_skeleton` 状态或允许 typesetting 与 designing 部分重叠
   3. typesetter 在 designing 阶段并行启动，先完成主题决策和骨架 HTML（无图片占位符）
   4. artwork_ready 后只填图片 src，重新校验
   5. 修改 `validate_article_layout.py` 支持分阶段校验（骨架校验 + 完整校验）
   6. 修改 `skill_run.py:complete()` 允许两阶段 complete（骨架 complete + 图片填充 complete）

   ```python
   # 示例：skill_run.py 新增 skeleton complete
   def complete_skeleton(args):
       """Register a skeleton HTML (without body images) for early typesetting."""
       # 校验骨架 HTML 结构正确，源文一致，但允许图片占位符
       # 状态从 skeleton_ready → artwork_pending
       pass
   
   def fill_images(args):
       """Fill body image src into the skeleton HTML after artwork_ready."""
       # 读取骨架 HTML，填入 manifest 中的 body image paths
       # 重新执行完整 validate_layout_output
       pass
   ```

**全部落地后预计：格式化 1.5min → 视觉并行 max(cover 2min, illustrator 5–7min) → 排版 2min → 发布 2.5min + leader 开销 1min ≈ 13–15 分钟。**

### 脚本合并映射表

| 现状 | 去向 | 状态 |
|---|---|---|
| 12× sha256_file、8× write_json、7× load_json、4× inside、3× tree_sha256/now_iso | `shared/hashing.py` + `shared/jsonio.py` | ✅已落地 |
| 3× frontmatter/title 解析 | `shared/markdown_meta.py` | ✅已落地 |
| formatter_skill_run + native_skill_run + layout_skill_run | `scripts/skill_run.py --boundary ...`（统一 reset 语义、role 单源） | ✅已落地 |
| runtime_integrity + release_integrity | `scripts/integrity.py` | ✅已落地 |
| stage_guard | `run_context.py guard` 子命令 | ✅已落地 |
| validate_formatted_content | `prepare_content.py validate` 子命令 | ✅已落地 |
| EXPECTED_SKILLS/ALLOWED_ROLES 双份 | skill_run.py 单源，validate_designer_manifest import | ✅已落地 |
| plugin_doctor 的 baoyu/gzh tree 校验 | 删除，移交 CI | ✅已落地 |

### 验收标准（改造后首次 run）

1. 全程墙钟 ≤18 分钟（串行插图）/ ≤15 分钟（并发插图）。— ⏳ 需实际 run 验证
2. 所有 Worker 的 complete 一次成功，零契约试错；leader 零救火、零手改状态文件。— ✅ 代码层面已保障
3. illustrator 若改写原文，在其 complete 时当场拒收（有测试用例）。— ✅ `test_illustrator_complete_rejects_rewritten_source_immediately`
4. 每次 run 全树 hash ≤3 次（init capture、发布前、published 门禁）。— ✅ 测试断言 `full_hash_count == 3`
5. `pytest tests/` 全绿；新增：B2 reset 语义、F2 role 前置校验、F3 源文一致性的回归测试。— ✅ 三项测试均已新增
6. token：leader cache_read <1.5M，全链 output <40k。— ⏳ 需实际 run 验证

---

## 落地进度总结

### 统计

| 状态 | 数量 | 占比 |
|------|------|------|
| ✅ 已落地 | 37 | 94.9% |
| ⚠️ 部分落地 | 1 | 2.6% |
| ❌ 未落地 | 1 | 2.6% |
| **合计** | **39** | **100%** |

### 各 Phase 完成度

| Phase | 项目数 | 已落地 | 部分落地 | 未落地 |
|-------|--------|--------|----------|--------|
| Phase 0 — 止血 | 8 | 8 | 0 | 0 |
| Phase 1 — 结构合并 | 8 | 8 | 0 | 0 |
| Phase 2 — 提示词瘦身 | 5 | 5 | 0 | 0 |
| Phase 3 — 并行化 | 3 | 1 | 1 | 1 |

### 关键成果

1. **脚本合并**：20+ → 14 个文件，7 个旧脚本全部删除
2. **shared 公共库**：建立 5 个共享模块（hashing/jsonio/markdown_meta/text_preservation/html_contracts）
3. **测试覆盖**：B2/F2/F3 三个关键回归测试均已新增，测试质量高
4. **安全门禁**：运行时完整性门禁已激活，每 run 全树 hash ≤3 次
5. **零试错设计**：complete 契约显性化 + role 前置校验 + amend-role 审计修复

### 待完成项

1. **⚠️ 插图并发生成**：codex-cli concurrency 2 → 3（代码改动 1 行），provider 级并行（需设计 per-task provider 分配策略）
2. **❌ typesetter 提前预热**：二期候选，需协议改动力度较大
3. **⏳ 墙钟/token 验收**：需实际 run 验证 35min → ≤18min 和 token -40% 的目标
