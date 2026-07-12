---
name: wechat-publisher
description: 把已经做好的内容发布到微信公众号草稿箱。支持微信贴图（article_type=newspic）和微信公众号文章（article_type=news）；文章可使用流水线已验收的 gzh-design HTML，旧 Markdown 渲染路径保留作显式兼容。当用户说「微信贴图一键发布」「把图片卡片发公众号」「发到公众号草稿箱」「发布公众号文章」时使用。只负责发布，不生成图片或重新排版。
---

# 微信公众号发布

把已经完成的图片卡片或已排版文章推送到微信公众号草稿箱，不做正式群发。

先解析 `PIPELINE_ROOT`：Claude Code 使用 `${CLAUDE_PLUGIN_ROOT}`；Codex 根据本 Skill 的 registry 绝对路径 `<PIPELINE_ROOT>/skills/wechat-publisher/SKILL.md` 向上两级得到。所有命令必须使用解析后的绝对路径。

## 职责边界

- `newspic`：纯文本正文 + 1-20 张本地图片，发布为微信贴图。
- `article --html`：发布通过 layout manifest 验收的 gzh-design HTML，上传正文图片和封面。
- `article <markdown>`：旧 Markdown renderer 的显式兼容路径，不是流水线 news 默认路径。
- 不生成图片、不修改正文、不补上游 manifest、不把草稿正式群发。
- HTTP Worker 是推荐通道；未配置 `WECHAT_PROXY_URL` 时才直连微信 HTTPS API。

## 必读参考

按当前任务读取，不要一次加载全部：

- newspic：`references/newspic.md`
- article：`references/article.md`
- 缺配置、账号不明确：`references/configuration.md`
- 网络、代理、API 错误：`references/transport.md`

## 固定工作流

1. 确认模式，只允许 `newspic` 或 `article`。
2. 解析输入并读取对应 reference。
3. 加载配置，顺序为：显式 `--env-file` → `WECHAT_PUBLISHER_ENV_FILE` → 输入旁 `.env.local/.env` → `~/.config/wechat-pipeline/.env.local/.env` → 进程环境变量覆盖。
4. 解析账号：`--account` → newspic 源文件 `account` → 唯一已配置账号。多账号但未指定时停止。
5. `article --html` 先运行 `${PIPELINE_ROOT}/scripts/validate_article_layout.py`；流水线运行必须提供 `--layout-manifest`。
6. 运行 dry-run 或展示确认信息。除非用户明确要求 `--yes`，发布前必须确认。
7. 使用完整绝对路径调用：

```bash
python3 "${PIPELINE_ROOT}/skills/wechat-publisher/scripts/publish.py" \
  <newspic|article> <arguments>
```

8. 成功时回报账号、模式和 `draft_media_id`；失败时回报脚本的真实错误。

## 重试语义

Publisher 代码会对连接错误、超时、HTTP 408/429/5xx 自动执行 `30/60/120` 秒退避。微信业务 `errcode` 不重试。代码完成退避后仍失败时，不要再次从 Agent 层循环调用整个发布流程。

## 安全

- 密钥只放在 `~/.config/wechat-pipeline/.env`、显式 env 文件或进程环境变量。
- 不读取后打印密钥，不把 token 写入日志、产物、Skill 或对话。
- 命名账号只读取 `WECHAT_<ACCOUNT>_*`，不会静默回退到全局 `WECHAT_*`；全局字段仅用于 `default` 单账号。

## 依赖

- Python 3.10+
- 仅旧 Markdown article 路径需要 Node.js + npm；`article --html` 只需要 Python 3.10+
- 旧 Markdown article 首次运行会把 npm 依赖安装到 `${CLAUDE_PLUGIN_DATA}/wechat-publisher/`；其他宿主使用 `~/.cache/wechat-pipeline/wechat-publisher/`
