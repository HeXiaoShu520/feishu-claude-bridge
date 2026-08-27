# Feishu Claude Bridge

飞书与 Claude Code SDK 之间的桥接服务，支持流式卡片实时更新。

## 支持

- 飞书 WebSocket 长连接收发文本；
- 所有飞书文本消息均会处理；
- Claude 会话按 `chat_id + user_open_id` 隔离并保存；
- `/help`、`/new`、`/stop`、`/resume [session_id]`、`/mode default|acceptEdits|plan|dontAsk`；
- Claude 工具请求以飞书卡片显示：允许一次 / 本会话允许 / 拒绝；
- 使用旧版 interactive 卡片，通过 `message.patch` 更新展示 Claude 输出；明确不使用飞书 CardKit JSON 2.0（V2）；
- 流式卡片使用颜色区分状态，终态正文末尾显示模型、上下文占用百分比、耗时和会话短 ID；上下文百分比来自 Claude Agent SDK 的 `get_context_usage()`；
- Claude 工具授权使用独立卡片显示，授权完成后继续更新原流式回复卡片；
- 同一 `chat_id + user_open_id` 复用 Claude client/子进程；服务重启或连接异常后通过保存的 session ID 恢复；
- 同一会话中，新普通消息默认中断旧任务后执行新任务。

不做多平台、Web 后台、provider、cron、多工作区、语音和 relay。

## 前置条件

- Node.js（用于 npm 命令）；
- Python 3.11+；
- 已安装并认证 Claude Code：

```powershell
claude login
```

## 配置

先复制模板：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_CLAUDE_CWD=E:/workspace/your-project
```

`.env` 已被 `.gitignore` 忽略，不会提交真实密钥。

飞书开放平台需启用机器人，并订阅：

```text
im.message.receive_v1
card.action.trigger
```

同时申请并发布以下能力所需权限：

- 创建和发送 IM 消息；
- 删除已完成的工具授权消息；
- 创建、更新和发送 CardKit 卡片实体。

流式回复明确不使用 CardKit 原生卡片实体、JSON 2.0（V2）或递增 `sequence` 更新；生产逻辑使用旧版 interactive `message.patch`，按钮也不使用 V2 专用结构。

终态指标示例：

```text
gpt-5.6-terra · 上下文 18.7% · 6.2s · 0514622c
```

其中上下文百分比由 Claude Agent SDK 的 `get_context_usage()` 返回，表示当前会话上下文窗口（系统提示词、工具、项目上下文、历史消息等）的使用比例，不是单轮输入 Token 占比。

使用“长连接”接收事件，不需要部署 HTTP Webhook。

## 安装、测试与启动

```powershell
npm run install
npm test
npm run start
# 开发启动（与 start 相同）
npm run dev
```

`npm run install` 会在项目内创建 `.venv`，再基于 `pyproject.toml` 安装 Python 依赖；不需要手动激活虚拟环境。

可将测试参数透传给 pytest：

```powershell
npm test -- -q
```

停止服务时按 `Ctrl+C`。

### 常见问题

- 报“未找到 Python 3”：安装 Python 3.11+，或设置 Python 可执行文件路径后再安装：

  ```powershell
  $env:PYTHON = "C:\Python312\python.exe"
  npm run install
  ```
- 报“未找到 .venv”：先执行 `npm run install`。
- Python 虚拟环境损坏：删除 `.venv` 后重新执行 `npm run install`。

## 安全

- `FEISHU_CLAUDE_CWD` 必须是受控目录；不要让聊天用户指定本机路径。
- 默认使用 `default` 权限模式；需要确认的工具会弹出飞书授权卡片。
- 可发送 `/mode dontAsk` 关闭授权卡片，但未预授权的工具会被拒绝；`acceptEdits` 只自动接受编辑类操作，其他工具仍可能需要确认。
- 不建议对普通用户开放 `bypassPermissions`；它会跳过权限确认并自动执行工具。
- 独立授权卡片只接受发起该 Claude 会话的用户点击，工具参数中的敏感字段会脱敏。
- 日志不会输出 App Secret、token、ticket、access_key、password、cookie 或 API key；飞书 SDK 的连接日志已关闭。请求日志只记录用户输入和模型输出的前 100 个字符。
- 不要把 `.env`、运行日志或截图提交到公开仓库；若凭证已经出现在日志中，应立即轮换。
- 建议用权限受限的操作系统账户运行机器人。

更多 SDK 使用说明见 [AGENT_SDK_使用说明.md](AGENT_SDK_使用说明.md)。
