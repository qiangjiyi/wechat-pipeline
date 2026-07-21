# WeChat Pipeline V2 需求

## 背景

当前 Plugin 已能完成格式化、配图、排版和发布，但控制流程主要依赖 Agent 遵守提示词。三次真实执行暴露出并发覆盖、跨运行污染、无效图片被标记成功、校验失败后继续发布、运行中修改校验代码等问题。

## 目标

用户安装 Plugin 后，只需提供一份原始草稿和目标模式，即可稳定生成一个经过回读验证的微信公众号草稿。

核心目标按优先级排序：

1. 正确：发布内容与原稿、图片清单和排版产物完全一致。
2. 可信：每个成功状态都由确定性脚本判定，Agent 不能自报或补造成功。
3. 可恢复：失败后复用同一 run，不重复创建草稿，不重做已验证阶段。
4. 高质量：格式化保留原意，视觉产物由指定原生 Skill 完整生成，排版保留全部可见文本。
5. 高效率：在不共享可变产物的边界内并行，避免无效等待和重复 Agent 会话。

## 用户与入口

- 用户提供本地文件或聊天正文。
- 用户可明确选择：
  - `newspic`：微信贴图；具体张数、比例和视觉方案由原生 Skill 决定。
  - `news`：公众号文章，包含 `2.35:1` 封面、正文配图和 gzh-design 排版。
- 用户可指定账号和明确的视觉偏好；未指定时由对应原生 Skill 决策。

## 标准流程

### 共同阶段

1. 封存原始输入。
2. 每次完整 Pipeline 都通过原生格式化 Skill 生成结构化 Markdown，不改写原意；不得因为原稿已有 H1 或 Markdown 结构而跳过，具体标题层级和表现结构由 Skill 自然决定。
3. 在独立 workspace 中完整调用模式对应的原生视觉 Skill。
4. 接收 Skill 最终返回结果并执行发布可用性验收。

### 微信贴图

5. 由 `baoyu-xhs-images` 自主完成微信贴图生成。
6. 冻结发布快照。
7. 发布到草稿箱并执行 `draft/get` 回读验证。

### 公众号文章

5. 将 `2.35:1` 作为发布场景选项交给 `baoyu-cover-image`；封面 Skill 与 `baoyu-article-illustrator` 在隔离 workspace 中并行执行，分别自主完成其余创作流程。
6. 只触发 gzh-design 并提供原生配图 Skill 返回的最终文章；主题、组件和内部工作流由 Skill 自主决定。Pipeline 只要求不新增作者签名或关注、点赞、在看、转发、分享等结尾引导，并在 Skill 完成后做确定性最终验收。
7. 联合验证原稿、图片 manifest、HTML、封面和正文图片引用。
8. 冻结发布快照。
9. 发布到草稿箱并执行 `draft/get` 回读验证。

## 功能需求

- 每次运行只有一个 `run_id` 和一个 canonical 输出目录。
- 原生视觉产物直接保留在运行根目录的 `<invocation-id>/`；Pipeline 不规定其中的 analysis、outline、prompt、图片或最终文章结构，实际子目录服从原生 Skill 的 `EXTEND.md`。
- `.pipeline/skill-runs/*.json` 只记录真实 Skill 标识、当前 `SKILL.md`、输入绑定、发布场景选项、开始/完成状态和 Skill 返回的最终结果。
- `init` 必须携带真实 source，禁止创建无输入 hash 的正常运行。
- 每个阶段只有一个产物所有者；其他 Agent 只读该阶段产物。
- 格式化 Worker 返回前必须完成只读自检并登记原生 Formatter Skill 成功回执；失败诊断包含原稿行号和片段预览，Leader 不得修改其 Markdown，也不得把 sealed 原稿作为 Formatter 产物绕过。
- 状态转换必须自动执行对应门禁，不能用普通 JSON 编辑替代。
- Formatter、visual、layout 使用统一的 start/complete/fail/reset 合同；失败恢复和历史 role 修复必须留下事件，禁止直接修改回执。
- Formatter 与 illustrator 的 complete 必须当场拒绝删除或改写源文；完整命令示例和 role 合同必须对 Worker 可见。
- Plugin 源码、第三方快照和 validator 在运行期间视为只读。
- 视觉 success 由目标原生 Skill 的准确身份、输入绑定、完整完成记录和最终返回文件 hash 判定；Pipeline 不判断内部创作过程，仅额外验证 news 最终封面满足 `2.35:1` 发布合同。
- HTML 中的本地图片必须与 manifest 声明的正文图片逐项一致。
- Publisher 只能消费不可变 publish snapshot。
- `draft/add` 不自动重试；草稿已创建但未验证时必须保留可恢复回执。
- 已发布运行可以离线重新审计，不依赖当前状态必须是 `publishing`。

## 非功能需求

- 正常运行不得使用 10 秒以上的阻塞 sleep。
- Agent 会话数量应稳定：每个 Skill invocation 每个 run 最多一个逻辑 worker；news 允许封面与正文配图两个 Worker 并行，会话恢复不创建平行 run。
- 图片生成是否并行以及如何 fallback 由被调用 Skill 自己决定；Pipeline 不覆盖其内部策略。
- 不读取、输出或写入真实密钥。
- 所有输出落在 `WECHAT_PIPELINE_EXPORTS_DIR` 对应类型目录；未配置时默认 `$HOME/Workspace/exports/`。

## 不做什么

- 不正式群发，只创建公众号草稿。
- 不在发布运行中修改 gzh-design 主题库或 Plugin 源码。
- 不用 placeholder、空白图或临时补图降级成成功。
- 不用第二套目录掩盖第一次运行失败。

## 验收标准

- 任一门禁失败时无法进入下一状态，且无法调用真实发布。
- 错误 Skill 路径/hash、缺失或失败的 Skill 完成记录、未由该 Skill 返回的最终文件均被拒绝。
- 排版 HTML 引用别名图片、未登记图片或遗漏正文图片均被拒绝。
- 发布包缺失、增加或修改任一受信文件时，Doctor/init 拒绝启动；运行中修改 Plugin 源码或 trust lock 时任务立即停止。
- 格式化删除语义字符、用 EXTEND 冒充 SKILL.md、缺少原生 Skill 最终结果、Skill 失败后手工补造 manifest 均被拒绝。
- 同一 publish snapshot 重试不会产生第二个草稿。
- newspic 与 news 各有一个完全离线的成功端到端测试。
- failed reset、错误 role、illustrator 改写源文、JPEG/WebP 封面比例和 provider 优先级均有回归测试。
- 首次真实 news run 的目标墙钟不超过 15 分钟；全树 hash 不超过 init、发布前、published 三次。
- 真实网络发布只在所有离线门禁通过后进行，并以回读验证作为唯一成功标准。
