# 图片理解接入与验证

2026-09-06，图片输入继续使用现有官方 app-server 0.153.2，无需图片生成用量补丁或新的上游 HTTP 分支。

## 支持范围

- 用户消息中的 `input_image`：内联 PNG/JPEG/WebP Base64；校验 Base64 和文件头是否匹配声明格式。
- 直接图片输入透传 `detail`：`auto`、`low`、`high`、`original`。
- `function_call_output` / `custom_tool_call_output` 支持原有字符串，以及 `input_text` / `input_image` 混合数组；工具调用 ID、所属用户、等待状态继续沿用原有校验。
- 图片工具结果转换为 app-server 的 `contentItems: inputText/inputImage`。该原生工具结果协议没有独立 `detail` 字段，工具图片沿用官方默认处理；不承诺工具回图可强制原始分辨率。
- 连续追问时图片及工具结果随用户自己的历史恢复，其他用户不能通过响应 ID 取回。
- nginx 和应用请求体上限均为 20 MiB（Base64 编码和历史也包含在内）。内部 RPC 行缓冲上限随既有历史上限及请求上限设置，避免原来 8 MiB 限制截断较大的历史事件。
- 不接受远程图片 URL、服务器文件路径或 Files API 文件 ID。读取本地图片由客户端执行，网关接收图片内容。

## 计量

使用上游返回的对话模型累计用量做增量结算，图片输入包含在其中；不叠加估算图片 token。延迟到工具回传后才收到的用量继续按现有补记逻辑结算。

当前对话用量未单独展示精确的视觉 token 明细，不把生图端点的 `usage` 当成看图用量。原有按输入字节估算的预扣仍保留，包含大图片的请求预扣可能明显高于最终消费，完成后按实际用量释放余额。

## 验证

- 本地 pytest：59 项通过，覆盖图片传递、原始清晰度参数、多图、工具图片历史恢复、跨用户隔离、增量扣费、无效内容和请求大小边界。
- 桌面版自带 Codex 0.153.0 执行程序：真实 Responses 客户端 → 网关 → 真实 app-server → 本地模拟模型。直接附图和内置 `view_image` 本地读图工具均通过，3 次模拟模型请求计量 330 token。测试使用独立配置、模拟上游和临时密钥，没有读取用户订阅；为避开 Windows 测试沙箱初始化，仅该测试客户端允许直接读取测试文件，shell/写文件工具关闭，生产服务权限未修改。
- VPS 官方 app-server 0.153.2：真实程序配合模拟模型，5 次网关请求共计 550 token，跨轮历史中的工具图片正常保留。
- VPS 现有订阅、`gpt-6-astra`、`medium`：直接附图识别“左红右蓝”；历史追问识别右侧为蓝色；客户端工具回图后同样识别正确，工具会话后续追问也正确。测试图片 2,097,863 字节，编码后请求超过旧 2 MiB 上限。
- 上述订阅测试使用独立账本：5 次网关请求共记 1,392 token，与上游总用量一致，其他测试用户用量为 0，未改四名生产用户额度；临时认证副本已删除。原始结果见 [VISION_LIVE_REPORT.json](VISION_LIVE_REPORT.json)。

复现脚本：`verify_vision.py --binary <app-server>` 为模拟上游，VPS 上加 `--live` 使用现有订阅和独立账本；`verify_vision_client.py <桌面自带codex.exe>` 验证真实桌面引擎的客户端协议。此验证覆盖桌面自带引擎，没有自动点击桌面 GUI 的全部附件交互。

## 部署

`upgrade_vision.py` 在无活动请求、工具等待窗口已结束时备份原账本与 nginx 配置，核对当前版本与已验证文件的哈希，切换版本后检查健康状态及账本摘要。失败会恢复原版本与 nginx 配置，不通过重置额度或覆盖账本来回滚。

已上线 `/opt/codex-relay-v2/releases/vision-20260906`，上个版本为 `context-continuity-final-20260906`。备份目录 `/opt/codex-relay-backups/vision-20260906-000053`（目录时间 UTC）。切换前后用户、密钥、预算、请求账本及计量总数摘要一致，未重置任何用户额度。

公网 HTTPS 实测通过：上传 2,797,439 字节的图片请求，正确识别左红右蓝；用 `previous_response_id` 追问，正确回复蓝色。两次请求分别记账 211、234 token，用户 1 共消费 445 token，均有上游用量依据且预扣归零。详见 [VISION_PUBLIC_REPORT.json](VISION_PUBLIC_REPORT.json)。
