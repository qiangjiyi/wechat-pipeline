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

自动重试范围：连接失败、超时、HTTP 408、HTTP 429、HTTP 5xx。退避为 30、60、120 秒，共最多四次请求。

以下错误不做网络重试：

- 微信业务 `errcode`
- HTTP 4xx（408/429 除外）
- 非 JSON 响应
- 输入、配置和本地文件错误

内部重试耗尽后，把最终错误返回调用方；不要从 Agent 层重新执行整个发布任务。
