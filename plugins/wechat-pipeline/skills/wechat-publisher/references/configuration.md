# Configuration

默认配置路径：`~/.config/wechat-pipeline/.env`。可调用 `wechat-pipeline:wechat-pipeline-setup` 创建权限 `0600` 的模板。

```env
WECHAT_PROXY_URL=https://your-proxy.example.com/
WECHAT_API_BASE=https://api.weixin.qq.com

WECHAT_ACCOUNTS=personal,company
WECHAT_PERSONAL_APP_ID=wx...
WECHAT_PERSONAL_APP_SECRET=...
WECHAT_COMPANY_APP_ID=wx...
WECHAT_COMPANY_APP_SECRET=...
```

账号别名会转为大写，并把非字母数字替换成下划线。命名账号必须使用对应的 scoped 字段。只有没有 `WECHAT_ACCOUNTS` 的 `default` 单账号可以使用：

```env
WECHAT_APP_ID=
WECHAT_APP_SECRET=
WECHAT_ACCESS_TOKEN=
```

配置优先级中，进程环境变量高于文件值。这允许 CI/临时会话覆盖配置；诊断时应检查 shell 中是否残留旧变量，但绝不能打印变量值。

