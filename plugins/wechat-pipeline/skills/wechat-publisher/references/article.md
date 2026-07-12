# Article Mode

流水线默认提供经过 gzh-design 和 layout manifest 验收的 HTML 正文片段。Publisher 上传其中的正文图片，只替换 `img[src]` 为微信返回的 mmbiz URL，保留其他结构和内联样式。

```bash
python3 "${PIPELINE_ROOT}/skills/wechat-publisher/scripts/publish.py" \
  article --html /absolute/run/article-body.html \
  --layout-manifest /absolute/run/.pipeline/layout.json \
  --account personal --yes
```

直接 Markdown 渲染是旧兼容路径：

```markdown
---
title: 文章标题
author: 作者名
coverImage: imgs/cover.png
description: 文章摘要
---

# 文章标题

正文段落。

![图片说明](imgs/inline-01.png)
```

Markdown 路径的元数据优先级：

- title：`--title` → frontmatter title → 第一个 H1/H2 → 文件名
- author：`--author` → frontmatter author
- cover：`--cover` → frontmatter cover hint → `imgs/cover.png` → 第一张正文图片

主题：`default`、`grace`、`simple`、`modern`。

颜色预设：`blue`、`green`、`vermilion`、`yellow`、`purple`、`sky`、`rose`、`olive`、`black`、`gray`、`pink`、`red`、`orange`，或 `#rrggbb`。

普通外链默认转为底部引用；`--no-cite` 保留内联链接。

```bash
python3 "${PIPELINE_ROOT}/skills/wechat-publisher/scripts/publish.py" \
  article /absolute/path/article.md --theme grace --color blue --account personal --yes
```
