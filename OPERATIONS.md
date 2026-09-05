# VPS 运维

- 服务：`codex-relay.service`，系统用户 `codex-relay`。
- 当前发布：`/opt/codex-relay-v2/current`。
- Python 环境：`/opt/codex-relay-v2/venv`。
- 官方二进制：`/opt/codex-relay-v2/bin/codex-app-server`。
- 账本：`/var/lib/codex-relay/relay.sqlite3`。
- 官方登录状态：`/var/lib/codex-relay/codex`。
- 工作目录：`/var/lib/codex-relay/work`（空项目环境）。
- nginx HTTPS 配置：`/etc/nginx/conf.d/codex-relay.conf`。
- 443 SNI 分流：`/etc/nginx/stream.d/yanzi-stream.conf`，只新增 relay 域名映射到 `127.0.0.1:9444`。
- 证书：`/etc/letsencrypt/live/relay.yanero.top/`，certbot 已设置自动续期及 nginx reload hook。
- DNS：Cloudflare 的 `relay.yanero.top` A 记录，DNS-only，TTL 300，指向 `203.31.199.37`。

旧 `/opt/codex-relay` 项目保留。上线前备份位于 `/opt/codex-relay-backups/pre-four-users-20260905.tar.gz`；旧 systemd/nginx 配置备份位于 `/opt/codex-relay-backups/configs-20260905`。备份中可能含旧凭据，只允许 root 读取。

## 状态

```sh
systemctl status codex-relay --no-pager
curl -fsS http://127.0.0.1:18021/healthz
journalctl -u codex-relay -n 60 --no-pager
nginx -t
```

FastAPI 单 worker；SQLite 和内存工具续接按单进程设计，不可直接扩成多个 worker。

## 登录和预算

管理员网页点击设备码登录，在 OpenAI 官方授权页完成操作，稍后刷新管理页。无需把订阅凭据发给四名用户。设备码登录期间不要重启服务，否则需要重新开始授权。

管理员可在网页修改每人预算、改名、停用、轮换个人密钥。停用会中断该用户活动会话；轮换阻止旧密钥发起新请求，已经开始的请求可能继续完成。待核对记录仅在有依据时手动结算；后续累计用量能解释工具段时会自动结算。

## 备份与更新

使用 SQLite 在线备份 API 或停服务后复制完整数据库；运行中不要只复制 `.sqlite3` 主文件而丢弃 WAL。官方 `auth.json` 和管理员初始密钥需要独立加密备份，不能放入源码归档。

发布新版本前运行 pytest 和 `fixture_protocol.py`，确认每个开放模型没有附带服务器工具。上传新发布目录、切换 `current`、重启服务；活跃的工具续接会失效，尽量在空闲期操作。调整默认模型使用 systemd 的 `CR_DEFAULT_MODEL`，模型名必须在网关目录中。

## 本地文件

源项目：`C:\Users\Administrator\codex-relay`。工作及验证副本：`D:\codex-relay-work`。私密接入文件：`D:\codex-relay-private`。

此部署未修改已有 Sub2API/VPN 服务。若它们使用相同上游订阅，其流量不会进入本中转账本，不能靠此账本限制；本项目不会自动删除这些服务的账号或配置。

## 管理员功能升级 · 2026-09-05

管理员功能首次发布为 `/opt/codex-relay-v2/releases/admin-tools-20260905`；其升级前 SQLite 在线备份及旧发布路径位于 `/opt/codex-relay-backups/admin-tools-20260905-132554`，仅 root 可读。升级采用追加表/字段迁移，保留原用户、密钥和账本。

新增接口均要求管理员 Bearer 密钥：

| 方法与路径 | 功能 |
| --- | --- |
| `POST /api/admin/usage/reset` | 重置全部用户已用量，保留历史和预扣 |
| `PATCH /api/admin/budget/all` | `{"budget":500000000}`，全部用户预算 |
| `GET /api/admin/statistics?start=…&end=…` | Unix 秒时间范围，起点包含、终点不包含 |
| `GET /api/admin/upstream/monitor` | 最近采样与比例估算 |
| `POST /api/admin/upstream/sample` | 手动采样，20 秒内合并重复请求 |

采样任务随服务启动，每 60 秒运行。`upstream_samples` 保留 35 天；`upstream_monitor_state` 保存最近成功状态与错误。连续超过 180 秒未成功采样时页面标记数据过期。无需浏览器或外部定时任务。`admin_actions` 记录批量重置/修改预算时间；`meter` 保存不受额度重置影响的官方用量累计观测值。

## 取消自动重置 · 2026-09-05

发布目录：`/opt/codex-relay-v2/releases/manual-quota-20260905`。升级前在线备份位于 `/opt/codex-relay-backups/manual-quota-时间戳`。从本次更新起用量和预扣跨周累计，只有管理员手动归零；`/api/me`、`/v1/usage`、管理员用户接口中的 `resets_at` / `period_start` 为 null，`reset_policy` 为 `manual`。旧 `period` 列仅为兼容原数据库保留，不参与预算计算。官方上游窗口的重置时间仍按官方数据展示。

TXT 示例位于 `cr/assets/codex-setup.txt`，也可从管理页下载。示例默认中档只设置接入客户端，不强制覆盖调用方显式指定的推理档位。
