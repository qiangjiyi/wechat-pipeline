# Transport And Errors

推荐配置 `WECHAT_PROXY_URL`，通过 HTTP Worker 信封调用微信 API：

- JSON：`POST { "url", "method", "data" }`
- 上传：`POST { "url", "method": "UPLOAD", "fileData", "fileName", "mimeType", "fieldName": "media" }`

未设置代理时，publisher 直连 `https://api.weixin.qq.com`。两种通道都会使用 HTTPS。

主要端点：

- `cgi-bin/token`
- `cgi-bin/material/add_material`
- `cgi-bin/draft/add`
- article 额外使用 `cgi-bin/media/uploadimg`

自动重试仅用于可安全重试的读取和素材上传：连接失败、超时、HTTP 408、HTTP 429、HTTP 5xx。退避为 30、60、120 秒，共最多四次请求。

`draft/add` 是非幂等写入，永不自动重试。若网络错误导致创建结果不确定，Publisher 写入 `creation_status: unknown` 回执并停止，恢复时不得再次调用 `draft/add`。人工确认草稿箱中的唯一草稿后，可传 `--recover-draft-media-id <media_id>` 绑定该草稿并执行回读验证。

以下错误不做网络重试：

- 微信业务 `errcode`
- HTTP 4xx（408/429 除外）
- 非 JSON 响应
- 输入、配置和本地文件错误

内部重试耗尽后，把最终错误返回调用方；不要从 Agent 层重新执行整个发布任务。
