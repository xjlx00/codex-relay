# VPS 运维

本轮修复的源码将总生成并发设为 6，并加入管理员可调的个人并发和预扣修正。本文的历史发布路径是既有记录，不代表本轮修复已经部署；真实客户端、上游 6 路容量和生产切换需另行验收记录。

- 服务：`codex-relay.service`，系统用户 `codex-relay`。
- 当前发布：`/opt/codex-relay-v2/current`。
- Python 环境：`/opt/codex-relay-v2/venv`。
- 官方二进制：`/opt/codex-relay-v2/bin/codex-app-server`。
- 账本：`/var/lib/codex-relay/relay.sqlite3`。
- 官方登录状态：`/var/lib/codex-relay/codex`。
- 工作目录：`/var/lib/codex-relay/work`（空项目环境）。
- nginx HTTPS 配置：`/etc/nginx/conf.d/codex-relay.conf`。
- 图片请求上限：nginx `client_max_body_size 20m`，应用 `Settings.max_body` 为 20 MiB，两处保持一致；上限包含 Base64 和对话历史。
- 443 SNI 分流：`/etc/nginx/stream.d/yanzi-stream.conf`，只新增 relay 域名映射到 `127.0.0.1:9444`。
- 证书：`/etc/letsencrypt/live/relay.yanero.top/`，certbot 已设置自动续期及 nginx reload hook。
- DNS：Cloudflare 的 `relay.yanero.top` A 记录，DNS-only，TTL 300，指向 `203.31.199.37`。

图片理解的历史发布记录为 `vision-20260906`，使用官方 app-server 0.153.2。切换前备份位于 `/opt/codex-relay-backups/vision-20260906-000053`，包含数据库、原 nginx 配置和原发布路径。恢复旧版时同时恢复其请求体上限；正常版本回退不覆盖当前账本。该次实测及计量明细见 [VISION.md](VISION.md)。

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

管理员可在网页修改每人预算、并发上限、名称、启用状态及轮换个人密钥。个人并发默认 1，只接受整数 1–6；`PATCH /api/admin/users/{id}` 的 `concurrent_limit` 保存到 SQLite 后更新调度器。上调立即放行满足条件的等待者；下调保留已运行请求，待运行数低于新上限再发放槽位。重启会重新载入每人的限额，普通用户不能修改。

全局最多 6 个生成请求；四人各设为 1 时最多同时运行 4 个。全局队列仍为 16 个、等待超时 120 秒，按有资格运行的用户轮转；工具等待不占生成槽，但计入每人 16 个活动/工具等待会话的上限。提高个人并发不会让同一活跃会话同时接受两个续接请求。

停用会阻止新准入并取消该用户排队、准备、生成和等待工具的请求；轮换阻止旧密钥发起新请求，已经开始的请求可能继续完成。已知消费会保留，只有未消费的预留继续显示为待核对预扣。待核对记录仅在有依据时手动结算；后续累计用量能解释工具段时会自动结算。

## 备份与更新

使用 SQLite 在线备份 API 或停服务后复制完整数据库；运行中不要只复制 `.sqlite3` 主文件而丢弃 WAL。官方 `auth.json` 和管理员初始密钥需要独立加密备份，不能放入源码归档。

发布新版本前运行 pytest 和 `fixture_protocol.py`，确认每个开放模型没有附带服务器工具。上传新发布目录、切换 `current`、重启服务；活跃的工具续接会失效，尽量在空闲期操作。调整默认模型使用 systemd 的 `CR_DEFAULT_MODEL`，模型名必须在网关目录中。

本轮数据库迁移只追加 `users.concurrent_limit INTEGER NOT NULL DEFAULT 1`，已有用户 ID、密钥、预算及账本保持不变。`requests.reserved` 继续保存原预留总额；用量接口的 `held` 和请求记录的 `reserved` 展示 `max(0, reserved - charged)`，没有重写旧预留值，也不需要反向迁移。旧代码仍能读取数据库，但会恢复原来的重复预扣缺陷和旧并发规则，回滚前应明确这些行为差异。正常代码回滚不得用旧数据库覆盖上线后新增消费。

发布前需验证数据库迁移与重启保留、预算边界、6 槽调度及取消释放、工具后自动压缩、最终正文和推理摘要。使用同模型/档位的真实客户端完成一次工具往返及压缩后续接，再记录真实上游并发和内存占用；当前 `MemoryMax=900M` 需要容量实测，调度上限 6 本身不能证明 VPS 或上游可承受 6 路。切换前应无排队、运行及等待工具会话，并备份完整 SQLite 和前一发布路径。

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

## 历史续接升级 · 2026-09-06

新增 response_history 表及索引，原用户、个人密钥、预算、用量和请求记录保持不变。默认历史保存 7 天，每用户快照正文 100 MiB，每用户活动/工具等待会话 16 个，工具等待 900 秒。参数在 cr/config.py；未引入 Redis，也不支持多 worker。

数据库从本版本起包含聊天正文、图片和工具结果。数据库保持服务账号所有、600 权限、服务 UMask=0077；源码包不包含数据库或凭据。数据库备份也含正文，应限制 root 访问并保留不超过 7 天。快照清理是逻辑删除，不保证 SQLite 空闲页/WAL/历史备份中的物理字节立即擦除；不要把“7 天”描述为所有副本的安全擦除承诺。

历史续接的发布记录为 `/opt/codex-relay-v2/releases/context-continuity-final-20260906`。该次最终切换前备份：`/opt/codex-relay-backups/context-continuity-20260905-181515`，目录时间为 UTC。该次验收见 VERIFICATION.md。

使用 upgrade_continuity.py 切换前，发布目录必须包含现有 cr 文件的 SHA-256 清单 baseline-hashes.json，以及全部开放模型协议测试通过后生成的 PROTOCOL_VERIFIED.json（models 结果和已验证 Python 文件/模型目录的 hashes）。脚本会拒绝源码已变更或验证不完整的发布，普通 package_release.py 归档不足以直接运行此升级脚本。

切换时先确认无排队/运行请求：旧版本要求最近请求已超过工具等待期限；有历史表的版本检查最近待工具快照已超过等待期限。停服务后再次检查，在线备份完整 SQLite 并记录前一发布，再原子替换 current 链接。启动健康检查失败则恢复旧代码；数据库迁移是追加式的，回滚不覆盖已产生的新用量。恢复旧代码后其内存续接仍不具备持久化恢复能力。

恢复数据库只用于明确的数据故障处置，不作为正常代码回滚步骤，避免覆盖新账本。恢复旧备份后先按 expires 清理快照。升级前的响应没有持久化正文，需要客户端重新提交完整历史；升级不会补造旧聊天记录。


## 本轮修复发布 · 0.3.0

已于北京时间 2026-09-06 09:14 左右上线 `/opt/codex-relay-v2/releases/repair-20260906`，前一版本 `vision-20260906`。完整备份位于 `/opt/codex-relay-backups/repair-20260906-011354/relay.sqlite3`。升级前后原用户、密钥、预算及账本摘要一致；上线公网测试另产生用户 1 的 163 token 已知消费，没有重置任何用量。

本轮使用 `upgrade_repairs.py`，要求 `REPAIR_VERIFIED.json` 和旧版本 `baseline-hashes.json`。本地与 VPS 各 159 项回归、6 路图文模拟容量、真实上游工具往返及公网 SSE 已通过，详见 REPAIR_REPORT.md。全局 6，个人默认 1，后台填写 1–6 并点击“保存并发”即时生效，SQLite 保留设置。

回滚只切回保留的旧代码并重启，保留当前 SQLite；不要覆盖数据库备份。旧代码可以读取新增用户并发列，但会回到旧调度行为；回滚不是继续保有本轮功能的替代版本。
