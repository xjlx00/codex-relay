# 四人接入

每个人只拿自己的 `userN.txt`。管理员使用 `admin.txt`。在 <https://relay.yanero.top/> 输入相应密钥，即可查看个人用量或管理四人预算。密钥不要放在 URL 中。

可下载 [Codex 接入示例 TXT](https://relay.yanero.top/assets/codex-setup.txt)，默认模型 `gpt-6-astra`，推理档位 `medium`（中档）。用量持续累计，仅管理员手动重置，无每周自动重置。

## 通用客户端

选择支持 **OpenAI Responses API** 的客户端，填写：

| 配置 | 值 |
| --- | --- |
| Base URL | `https://relay.yanero.top/v1` |
| API Key | 自己的 `userN.txt` 中的 `cr_...` 密钥 |
| 默认模型 | `gpt-6-astra` |
| API 类型 | Responses |

可以用 CC Switch 管理这个配置；不要求使用 CC Switch、Claude Code 或原生 Codex。只支持 Chat Completions 的客户端不能直接使用此版本。

## Python 文本调用

先将个人密钥设为环境变量 `CODEX_RELAY_API_KEY`，然后运行：

```python
import os
import httpx

r = httpx.post(
    "https://relay.yanero.top/v1/responses",
    headers={"Authorization": "Bearer " + os.environ["CODEX_RELAY_API_KEY"]},
    json={"model": "gpt-6-astra", "input": "请简要介绍这个项目。"},
    timeout=750,
)
r.raise_for_status()
print(r.json())
```

流式调用增加 `"stream": true`，按 SSE 读取 `response.output_text.delta` 和 `response.completed`。流开始后发生的错误通过 `response.failed` 返回，客户端必须检查最终事件，不能只检查 HTTP 200。

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

若返回 `function_call`，由客户端自行检查路径和权限、在本人的电脑上执行，然后将实际结果作为字符串发送到同一 `/v1/responses`。保留相同 `model`、`tools` 和 `instructions`，`input` 提供全部未完成的工具结果：

```json
[
  {"type": "function_call_output", "call_id": "从响应取得的 call_id", "output": "实际 README 内容"}
]
```

网关不替客户端执行命令。客户端自身仍应保留命令审批和文件权限检查。

## 常见错误

| 错误 | 处理 |
| --- | --- |
| `401 unauthorized` | 检查个人密钥是否正确、被停用或更换 |
| `403 forbidden` | 管理员/个人密钥用途不同 |
| `429 budget_exhausted` | 查看当前额度和待核对预扣 |
| `429 queue_timeout_or_full` | 稍后重试，减少并发 |
| `503 authentication_required` | 管理员需要完成订阅登录 |
| `409 continuation_lost` | 服务重启或工具等待过期，重发完整历史 |
| `404 previous_response_not_found` | 该历史已过期或不属于本人 |
| `400 model_not_supported` | 使用 `GET /v1/models` 返回的模型 |

预算是本中转的分配规则。官方账号额度先耗尽时，四人都会受到上游限制。

## 管理员批量操作与统计

刷新网页并使用管理员密钥登录，在“批量额度管理”中可一次设置所有用户的预算，或确认后重置所有用户已用额度。重置保留历史统计、在途请求和待核对预扣；重置后产生的新用量继续扣账。批量预算包含已停用用户，不会自动启用他们。

用量统计支持自选起止时间及今天、本周、最近 7 天快捷范围。时间按北京时间解释，按请求开始时间归入区间，包含起点、不包含终点。缓存 token 已包含在输入 token 中；尚未结算的用量随后补入原请求的统计。

上游额度由服务器每 60 秒自动采样，关闭网页仍继续；打开管理员页面时每 15 秒刷新显示，也可手动采样。分别展示官方提供的各额度窗口剩余百分比。主 Codex 额度池在同一观察区间累计消耗至少 1 个百分点且本网关有新增用量后，显示“新增 token ÷ 消耗百分点”的估算。账号或套餐变化、额度重置、百分比下降、采样中断超过 3 分钟时重新积累数据。其他额度池只展示剩余量。

该比例是观察估算：模型、缓存比例、官方上报延迟，以及其他入口使用同一个账号，都会影响结果；它不代表官方固定的 token 兑换额度。
