# Codex Relay：四人私人中转

基于官方 `codex-app-server 0.153.2` 的 Responses 网关。默认模型 `gpt-6-astra`，4 个独立用户；另有独立管理员密钥。初始化预算默认每人 500,000,000 token，可由管理员修改。2026-09-05 最近一次部署核验为每人 400,000,000 token，实际值以管理页面为准。

- 用量及管理页面：<https://relay.yanero.top/>
- API Base URL：`https://relay.yanero.top/v1`
- 接入说明：[USERS.md](USERS.md)
- Codex 接入 TXT：[codex-setup.txt](cr/assets/codex-setup.txt)，默认 `gpt-6-astra` / `medium`。
- 设计及限制：[DESIGN.md](DESIGN.md)
- 运维说明：[OPERATIONS.md](OPERATIONS.md)

本项目集中保管上游凭据、隔离用户会话并限制内部预算。网关不会额外把个人网关密钥、内部用户编号或预算加入模型请求；用户提交的任务正文、工具及上下文仍会发给上游。统一 VPS 出口不保证 ChatGPT/OpenAI 无法识别账号共享，也不增加订阅账号的官方额度；本项目保留真实集成标识，不伪装成官方桌面客户端。

全局生成并发为 6，每用户默认 1，管理员可在后台逐人调整为整数 1–6，保存后立即生效且重启保留。四名用户都保持 1 时，最多同时生成 4 个请求。客户端执行工具时释放生成槽，工具结果回传后重新申请；并发 1 通常足够单会话、无子代理的顺序生成流程，整体体验仍受上游共享额度、网络和协议兼容性影响。

内部用量持续累计，仅管理员手动重置，不再按北京时间周一自动归零。已知消费与未消费的预留分别计入已用量和预扣；断线或重启后的未知部分保留为待核对，不再将已知消费重复预扣。

响应历史持久化到现有 SQLite，包含正文、图片和工具结果：默认 7 天、每用户 100 MiB，容量不足会提前淘汰旧快照。有效的已完成响应可在网关重启后恢复；快照过期或被淘汰后，仅有响应 ID 无法续接，需客户端重发完整历史。工具等待仍为 15 分钟；历史内容与清理边界见 [DESIGN.md](DESIGN.md)。

本轮已发布为 0.3.0（repair-20260906）。159 项本地及 VPS 回归、新旧客户端格式与默认 Code Mode、服务器 6 路图文模拟模型容量、真实上游工具往返和公网 SSE 验收通过。6 路测试不是官方订阅同时提供 6 路能力或桌面全部功能的保证。详情见 [REPAIR_REPORT.md](REPAIR_REPORT.md)。

## 接口

| 接口 | 用途 |
| --- | --- |
| `POST /v1/responses` | 文本、图片理解、流式响应、客户端工具及图片结果回传 |
| `GET /v1/models` | 网关开放的模型列表 |
| `GET /v1/usage` | 当前个人用量 |
| `POST /v1/responses/{id}/cancel` | 取消自己的排队、准备、生成或等待工具的请求 |
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

图片接入与验证结果见 [VISION.md](VISION.md)。支持 Base64 PNG/JPEG/WebP，单个 HTTP 请求上限 20 MiB；直接附图和客户端看图工具均走现有 app-server。
