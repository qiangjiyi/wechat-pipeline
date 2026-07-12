# Article Mode

必须提供 Markdown 路径。渲染器使用 `baoyu-md` 生成微信兼容 HTML，并把正文图片占位符替换为微信返回的 mmbiz URL。

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

优先级：

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

