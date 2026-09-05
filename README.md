# Codex Relay：四人私人中转

基于官方 `codex-app-server 0.153.2` 的 Responses 网关。默认模型 `gpt-6-astra`，4 个独立用户；另有独立管理员密钥。初始化预算默认每人 500,000,000 token，可由管理员修改。2026-09-05 最近一次部署核验为每人 400,000,000 token，实际值以管理页面为准。

- 用量及管理页面：<https://relay.yanero.top/>
- API Base URL：`https://relay.yanero.top/v1`
- 接入说明：[USERS.md](USERS.md)
- Codex 接入 TXT：[codex-setup.txt](cr/assets/codex-setup.txt)，默认 `gpt-6-astra` / `medium`。
- 设计及限制：[DESIGN.md](DESIGN.md)
- 运维说明：[OPERATIONS.md](OPERATIONS.md)

本项目集中保管上游凭据、隔离用户会话并限制内部预算。统一 VPS 出口不保证账号共享无法被服务方识别，也不增加订阅账号的官方额度。

内部用量持续累计，仅管理员手动重置，不再按北京时间周一自动归零。

## 接口

| 接口 | 用途 |
| --- | --- |
| `POST /v1/responses` | 文本、流式响应、客户端工具调用 |
| `GET /v1/models` | 网关开放的模型列表 |
| `GET /v1/usage` | 当前个人用量 |
| `POST /v1/responses/{id}/cancel` | 取消自己的进行中请求 |
| `GET /api/me` | 本人的身份、用量及请求记录 |
| `/api/admin/*` | 管理员专用的预算、密钥和订阅登录接口 |

模型请求使用个人密钥；管理员密钥不能用于模型请求。此版本实现 Responses API 的明确子集，不提供 Chat Completions，也不是完整 OpenAI API 替代品。

## 本地验证

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q --basetemp .test-temp-new
```

`fixture_protocol.py` 在 Linux 上启动本地模拟 Responses 接口，让真实官方二进制连接模拟接口，验证文本、客户端工具往返和用量。它不连接真实模型、不读取已有订阅凭据。生产真实请求必须在管理员完成设备码登录后另行验证。

## 凭据

网关数据库只保存个人密钥的 SHA-256 摘要。初始化明文密钥保存在 VPS 的 `/root/codex-relay-initial-keys.json`（权限 600），本地交付文件放在项目外的 `D:\codex-relay-private`（仅 Administrator/SYSTEM 可访问）。不要将这些文件提交到代码仓库。

Cloudflare 令牌及 SSH 密码未写入项目或部署配置。
