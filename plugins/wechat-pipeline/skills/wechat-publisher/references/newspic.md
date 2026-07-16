# Newspic Mode

输入可以是 `.md`、`.yaml`、`.yml`、`.json`，也可以显式传 `--title`、`--content` 和重复的 `--image`。未提供输入时读取当前目录 `source.md`。

YAML/frontmatter 使用有意受限的平面格式：支持顶层标量和缩进列表，不支持嵌套对象、多行标量或行尾注释。需要复杂结构时使用 JSON；`#` 可能是正文或密钥的一部分，因此解析器不会猜测并剥离行尾内容。

推荐格式：

```markdown
---
account: personal
author: "即刻内容工作室"
images:
  - cards/card-01.png
  - cards/card-02.png
---

# 这周值得收藏的 AI 工具

整理成 6 张卡片，适合快速看完。
```

约束：

- `title` 最多 20 字符，H1 优先于缺失的 frontmatter title。
- `content` 是纯文本，最多 1200 字符。
- `images` 必须包含 1-20 个存在的本地文件。
- `--image` 可重复，并替换源文件中的 images。

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/skills/wechat-publisher/scripts/publish.py" \
  newspic /absolute/path/source.md --account personal --yes \
  --result-output /absolute/run/.pipeline/publish-result.json --verify-draft
```

完整流水线固定绑定已验收 manifest，不允许独立覆盖正文或图片：

```bash
"${PIPELINE_ROOT}/scripts/run_python.sh" "${PIPELINE_ROOT}/skills/wechat-publisher/scripts/publish.py" \
  newspic --manifest /absolute/run/.pipeline/manifest.json \
  --account personal --yes \
  --result-output /absolute/run/.pipeline/publish-result.json --verify-draft
```
