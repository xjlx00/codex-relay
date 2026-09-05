# 原生 app-server 图片用量实测

2026-09-06（北京时间），使用现有 ChatGPT Pro 订阅，经补丁版 app-server 原生生图工具真实生成一次图片，成功取得完整图片 token 用量。

## 实测结果

| 项目 | token |
| --- | ---: |
| 图片请求输入 | 60 |
| 输入中的文字 | 60 |
| 输入中的图片 | 0 |
| 图片请求输出 | 229 |
| 输出中的文字 | 0 |
| 输出中的图片 | 229 |
| 图片请求合计 | 289 |

原始 `item/completed` 事件中的 `item.type` 为 `imageGeneration`，`item.status` 为 `completed`，新增 `item.usage` 为：

```json
{
  "input_tokens": 60,
  "input_tokens_details": {"image_tokens": 0, "text_tokens": 60},
  "output_tokens": 229,
  "output_tokens_details": {"image_tokens": 229, "text_tokens": 0},
  "total_tokens": 289
}
```

脚本验证了三个总数字段及两组分类明细均存在、均为非负整数，且各项相加一致。缺失字段保留为缺失，不估算为零。此次恰好收到一项成功图片结果，整个验证耗时约 23.9 秒。

普通对话模型为 `gpt-6-astra`，推理档位为 `medium`。它在调用工具前后另产生 3,652 token（输入 3,568、输出 84）。该数字来自两次普通 Responses 的用量，与图片事件中的 289 token 分别记录。如预算规则按两者直接相加，本次为 3,941 token；这不代表不同模型 token 的价格或订阅限额权重相同。

![实测生成的白底绿色方块](live-image.png)

## 调用链路与变更

验证脚本通过 stdio JSON-RPC 调用独立 app-server，普通模型调用原生 `image_gen.imagegen`，app-server 自带的 `ImagesClient` 请求原生 `images/generations` 端点。未引入 Sub2API 或另写上游 HTTP 请求分支。

补丁在 `ImageResponse` 中接收原始 `usage` JSON，传递至图片扩展条目，并由公开的图片完成事件输出。未知或未返回的用量保持 `null`，旧记录能够继续反序列化；旧版 legacy image 事件没有新增用量。上游请求体、认证方式和请求头均未修改。

本次实测证明这个订阅、端点和请求返回了完整用量，不保证所有模型、编辑请求或失败响应也一定返回。原生图片编辑已在代码解析层纳入传递，但未再消耗订阅进行真实编辑测试。

## 可复现材料

- 上游源码：OpenAI/Codex `52e73e3a548ae5310c7765995b9803dd538b82b0`。
- [构建记录](https://github.com/xjlx00/codex-relay/actions/runs/33983405209)。
- 构建工作流提交：`f49a2ac`。
- 独立二进制 SHA-256：`2938a8e5a8f5b6fbf6dc58a4a4c309cd091cd2df20e4228d8c59185e1bed0aa1`。
- 二进制版本显示 `0.0.0`，对应固定源码的自编译产物，不冒充官方发行版。
- [完整补丁（含 schema）](complete-source.patch)、[探测脚本](probe_patched_app_server.py)、[原始用量与图片完成事件](live-report.json)。报告删去无关事件和图片 Base64，保留实际用量、关联 ID 和校验和，不含认证凭据。
- 图片 SHA-256：`20d96b34538929e14d53b12d24f909899989fdd13a0d29e4d1b2db637d336542`，大小 755,856 字节。

## 测试与部署范围

[最终测试复验](https://github.com/xjlx00/codex-relay/actions/runs/33984645232)已通过：基础解析与序列化 13 项、协议及 schema 298 项、图片集成 8 项，共 319 项。图片集成测试包括新增的完整用量传递断言，以及生成、编辑、失败事件和历史记录兼容。

构建机最初缺少 `bubblewrap`，导致本地附件读取测试失败；安装后已通过。Code Mode 执行图片测试未运行：其 V8 150.4.0 预编译依赖下载返回 HTTP 404；本次实测直接使用原生工具，不依赖该程序。最终复验只增加构建机依赖，源码补丁与上述真实验证时相同，没有再次生成真实图片。

测试进程与线上服务隔离，使用临时 CODEX_HOME，结束时删除临时认证副本。本次脚本没有写入生产用户计量数据库。线上仍运行官方 app-server 0.153.2，尚未替换生产二进制，尚未接入网关转发或每用户图片扣费。
