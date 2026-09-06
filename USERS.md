# 四人接入

每个人只拿自己的 `userN.txt`。管理员使用 `admin.txt`。在 <https://relay.yanero.top/> 输入相应密钥，即可查看个人用量或管理四人预算。密钥不要放在 URL 中。

可下载 [Codex 接入示例 TXT](https://relay.yanero.top/assets/codex-setup.txt)，默认模型 `gpt-6-astra`，推理档位 `medium`（中档）。用量持续累计，仅管理员手动重置，无每周自动重置。

本轮规则已于 2026-09-06 上线，版本 0.3.0。后台用户卡填写“并发上限”并点击“保存并发”即可即时生效。完整验收范围见 REPAIR_REPORT.md，尚未覆盖桌面版全部功能。

## 并发与使用体验

全局生成并发为 6，每人默认 1。管理员可在后台把个人上限调整为 1–6，保存后立即生效并在重启后保留；下调不会中断已经运行的请求。四名用户都保持 1 时，最多同时生成 4 个请求。

只使用一个会话、不调用子代理时，通常按“模型生成 → 本地执行工具 → 模型继续生成”依次进行，个人并发 1 足够这一流程。它限制同时进行的模型请求，不限制本地工具只能执行一个。工具执行期间释放生成槽，回传结果后重新申请；同一活跃会话不能同时提交多个续接请求，不同窗口请分别保留自己的历史。

中转网络、其他用户占用的总槽位和共享的上游额度仍会影响等待时间；并发 1 不代表已与官方桌面版直接登录的完整体验等同。统一出口不能保证 ChatGPT/OpenAI 无法识别账号共享，本项目保留真实集成标识，不伪装官方客户端。

## 通用客户端

选择支持 **OpenAI Responses API** 的客户端，填写：

| 配置 | 值 |
| --- | --- |
| Base URL | `https://relay.yanero.top/v1` |
| API Key | 自己的 `userN.txt` 中的 `cr_...` 密钥 |
| 默认模型 | `gpt-6-astra` |
| 默认推理档位 | `medium`（中档） |
| API 类型 | Responses |

可以用 CC Switch 管理这个配置；不要求使用 CC Switch、Claude Code 或原生 Codex。只支持 Chat Completions 的客户端不能直接使用此版本。

## Windows 桌面版接入注意事项

用户级配置默认位于 `%USERPROFILE%\.codex\config.toml`；若指定了 `CODEX_HOME`，以实际配置目录为准。使用 TXT 中的自定义 provider 配置时，顶层模型/档位设置放在 `[model_providers.yanero_relay]` 之前，与已有配置合并，不重复添加同名项。不要把 TXT 的分隔说明或 Markdown 链接格式写进 TOML。

在 Windows 搜索“编辑账户的环境变量”，新增用户变量 `CODEX_RELAY_API_KEY`，值为自己的完整个人密钥。完全退出并重新打开 Codex，新建任务检查 `gpt-6-astra / medium`。若进程仍读不到新变量，可注销 Windows 后重新登录。PowerShell 的 `$env:CODEX_RELAY_API_KEY=...` 只影响该终端及其随后启动的子进程，不保证从桌面图标打开的应用能读取。

接入失败时先核对 Base URL 末尾的 `/v1`、`wire_api = "responses"`、顶层模型设置的位置，以及 `env_key` 是否为 `CODEX_RELAY_API_KEY`。下面的 PowerShell 只显示是否设置了变量，不打印密钥；若用户变量为 True、当前进程为 False，关闭旧终端及 Codex 后重新打开再试。

```powershell
[pscustomobject]@{
    UserKeyConfigured = [bool][Environment]::GetEnvironmentVariable('CODEX_RELAY_API_KEY', 'User')
    ProcessKeyLoaded = [bool]$env:CODEX_RELAY_API_KEY
}
```

已有验证涵盖网关 Responses、流式响应和客户端函数往返；尚未完成 Codex 桌面版所有工具与功能的端到端实测。配置字段正确不代表所有桌面功能均已验证兼容。参考：[OpenAI 自定义 provider 配置](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)。

## Python 文本调用

先将个人密钥设为环境变量 `CODEX_RELAY_API_KEY`，然后运行：

```python
import os
import httpx

r = httpx.post(
    "https://relay.yanero.top/v1/responses",
    headers={"Authorization": "Bearer " + os.environ["CODEX_RELAY_API_KEY"]},
    json={"model": "gpt-6-astra", "reasoning": {"effort": "medium"}, "input": "请简要介绍这个项目。"},
    timeout=750,
)
r.raise_for_status()
print(r.json())
```

流式调用增加 `"stream": true`，按 SSE 读取 `response.output_text.delta` 和 `response.completed`；请求公开推理摘要后，可读取 `response.reasoning_summary_text.delta` 观察进度。流开始后发生的错误通过 `response.failed` 返回，客户端必须检查最终事件，不能只检查 HTTP 200。主动取消也以 `response.failed` 结束流，其中 `response.status` 为 `cancelled`、`error.code` 为 `cancelled`；管理员停用时错误码为 `user_disabled`。

## 函数工具示例

首个请求声明工具：

```json
{
  "model": "gpt-6-astra",
  "input": "读取 README.md 并总结。",
  "tools": [{
    "type": "function",
    "name": "read_file",
    "description": "读取用户本地项目文件",
    "parameters": {
      "type": "object",
      "properties": {"path": {"type": "string"}},
      "required": ["path"]
    }
  }]
}
```

若返回 `function_call`，由客户端自行检查路径和权限、在本人的电脑上执行，然后将实际结果作为字符串发送到同一 `/v1/responses`。保留相同 `model`、`tools`、`instructions` 和推理/输出格式等生成设置，`input` 提供全部未完成的工具结果：

```json
[
  {"type": "function_call_output", "call_id": "从响应取得的 call_id", "output": "实际 README 内容"}
]
```

网关不替客户端执行命令。客户端自身仍应保留命令审批和文件权限检查。

经典格式携带顶层 `tools` 时，`parallel_tool_calls=false` 会明确返回 400：当前 app-server 桥接无法保证单次响应只调用一个工具。不要把个人生成并发 1 当作工具串行保证；需要串行处理时由客户端控制工具执行。`max_output_tokens` 及强制指定工具也不受支持，不能依靠这些参数限制上游执行。

## 历史保存与窗口

普通已完成响应的历史默认在中转保留 7 天，每用户最多 100 MiB 快照正文；本人使用量达到存储上限时，会提前清理本人的旧快照。重启中转后，仍可用有效的 previous_response_id 续接。客户端应继续保存自己的长期聊天历史。

新对话发送自身 input；续接某个对话发送它的 previous_response_id 和新增 input。不同窗口各用各的历史/ID。同一请求已带完整历史时可以不传 previous_response_id；不要在旧 ID 失效后仅删除 ID 却仍只发送最新一句话。

工具结果和新增 user 消息可一起提交，新消息会在模型继续生成前注入。若返回“New messages could not be injected”，原工具仍在等待，可先提交工具结果，再单独发新消息。接口不会静默忽略新增消息。

中转的恢复存储包含对话正文、图片和工具结果；请求中的 store=false 不关闭这项恢复存储。快照过期或因容量淘汰后，仅凭旧响应 ID 无法续接，须重发客户端保存的完整历史。普通用户的用量页面不提供其他用户的历史或正文。切换个人密钥时也应新建/清理客户端会话，服务端不能识别客户端自行带来的历史属于哪位前任使用者。

## 常见错误

看图可直接在客户端附加图片，也可让客户端的看图工具读取游戏项目中的图片。客户端必须有读取文件的权限，并把图片内容传给网关；只发送本机路径无法让 VPS 读取图片。支持 PNG/JPEG/WebP Base64 图片及文字/图片混合工具结果，整个请求（含 Base64 和历史）最多 20 MiB。`detail` 可用于直接附图；工具图片使用 app-server 原生工具结果默认的清晰度处理。

看图按该轮上游返回的总输入/输出 token 计入个人预算，不另加估算的图片 token。图片内容同其他对话历史一样受 7 天保留期及每用户 100 MiB 历史容量约束。生图属于另一条功能链路，本次看图升级没有启用生图接口。

请求准入时，网关对每张图片暂按 65,536 token 预留，加上文本、工具定义和输出预留；工具图片和恢复历史中的图片也计算在内。Base64 编码长度不再直接当成模型 token。此值是保守的预算预留，完成后仍只扣上游实际用量，不代表图片固定收费或所有未来模型的硬上限。

| 错误 | 处理 |
| --- | --- |
| `401 unauthorized` | 检查个人密钥是否正确、被停用或更换 |
| `403 forbidden` | 管理员/个人密钥用途不同 |
| `429 budget_exhausted` | 查看当前额度和待核对预扣 |
| `429 queue_full` | 等待队列已满；稍后重试，减少同时提交的请求 |
| `429 queue_timeout` | 等待生成槽超过 120 秒；稍后重试或联系管理员调整个人并发 |
| `503 authentication_required` | 管理员需要完成订阅登录 |
| `409 continuation_lost` | 工具等待已失效或结果已消费；核实已执行操作，必要时移除旧 ID 并发送成对工具调用/结果的完整历史，不盲目重跑工具 |
| `409 continuation_busy` | 同一活跃会话已有续接在处理；等待该请求完成，避免重复提交工具结果 |
| `409 response_history_required` | 本人的快照已清理或来自升级前；移除旧 previous_response_id，重发完整历史 |
| `404 previous_response_not_found` | 响应 ID 未知或不属于本人 |
| `413 response_history_capacity` | 本次生成的历史快照超过每用户 100 MiB 上限，不能作为成功续接点 |
| `429 session_capacity` | 本人活动/工具等待会话已达到 16 个，完成或取消旧会话后重试 |
| `400 model_not_supported` | 使用 `GET /v1/models` 返回的模型 |

“已用量”显示已知消费，“预扣”只显示尚未消费的预留。例如预算 1000、预留 900、已消费 100 时，已用为 100、预扣为 800，可用于新请求的余额为 100；正在运行的请求可继续使用自己的预留。断线或重启时保留已知消费，无法确认的剩余预留显示为待核对。预算是本中转的分配规则，官方账号额度先耗尽时四人都会受到上游限制。

## 管理员批量操作与统计

刷新网页并使用管理员密钥登录，在“批量额度管理”中可一次设置所有用户的预算，或确认后重置所有用户已用额度。重置保留历史统计、在途请求和待核对预扣；重置后产生的新用量继续扣账。批量预算包含已停用用户，不会自动启用他们。

用量统计支持自选起止时间及今天、本周、最近 7 天快捷范围。时间按北京时间解释，按请求开始时间归入区间，包含起点、不包含终点。缓存 token 已包含在输入 token 中；尚未结算的用量随后补入原请求的统计。

上游额度由服务器每 60 秒自动采样，关闭网页仍继续；打开管理员页面时每 15 秒刷新显示，也可手动采样。分别展示官方提供的各额度窗口剩余百分比。主 Codex 额度池在同一观察区间累计消耗至少 1 个百分点且本网关有新增用量后，显示“新增 token ÷ 消耗百分点”的估算。账号或套餐变化、额度重置、百分比下降、采样中断超过 3 分钟时重新积累数据。其他额度池只展示剩余量。

该比例是观察估算：模型、缓存比例、官方上报延迟，以及其他入口使用同一个账号，都会影响结果；它不代表官方固定的 token 兑换额度。


Codex 新版 Responses Lite 兼容：接受首个 developer additional_tools 包装，将其中工具声明经过原有客户端工具校验与别名映射后使用，不把原名工具重复暴露给上游。Codex 在该协议自动发送 parallel_tool_calls=false；网关按协议适配，不能将此固定值视作用户要求工具串行。经典格式带顶层 tools 且 false 仍明确拒绝。此区别不影响全局及个人生成并发限制。
