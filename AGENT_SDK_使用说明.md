# Claude Agent SDK：怎么用、能用什么

这是 Python 的 `claude-agent-sdk`。它通过本机 Claude Code 工作，不是普通 API 调用。

## 先准备

```powershell
claude login
python -m pip install claude-agent-sdk
```

项目目录里有 `CLAUDE.md` 时，只要设置 `cwd`，Claude Code 会自动读取它。

---

## 最简单使用

```python
import asyncio
from claude_agent_sdk import ClaudeAgentOptions, query


async def main():
    options = ClaudeAgentOptions(
        cwd="E:/源丶工程/cc-connect",
        permission_mode="plan",
    )

    async for message in query(
        prompt="分析当前项目",
        options=options,
    ):
        print(message)


asyncio.run(main())
```

适合一次性任务：发一句 prompt，等 Claude 完成。

---

## 聊天机器人应使用的方式

飞书机器人用 `ClaudeSDKClient`，因为它支持多轮、打断和持续接收输出。

```python
import asyncio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient


async def main():
    options = ClaudeAgentOptions(
        cwd="E:/源丶工程/cc-connect",
        permission_mode="default",
        include_partial_messages=True,
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query("分析当前项目")

        async for message in client.receive_response():
            print(message)


asyncio.run(main())
```

核心 API：

| API | 用途 |
|---|---|
| `await client.query(text)` | 发送用户消息给 Claude |
| `async for item in client.receive_response()` | 持续接收 Claude 文本、工具和完成事件 |
| `await client.interrupt()` | 中断当前任务 |
| `ClaudeAgentOptions(resume=session_id)` | 恢复旧 Claude 会话 |
| `ResultMessage.session_id` | 获取并保存本轮 Claude 会话 ID |

**每次 `query()` 后必须消费 `receive_response()` 直到结束。**

---

## 飞书项目最重要的功能

### 1. 工作目录

```python
ClaudeAgentOptions(cwd="E:/workspace/my-project")
```

Claude 在这个目录工作，并自动读取该目录的 `CLAUDE.md`。

### 2. 连续会话

同一个 client 可连续聊天：

```python
await client.query("看项目")
async for item in client.receive_response():
    ...

await client.query("继续看登录模块")
async for item in client.receive_response():
    ...
```

任务结束后保存：

```python
session_id = result.session_id
```

服务重启后恢复：

```python
options = ClaudeAgentOptions(
    cwd=workspace,
    resume=session_id,
)
```

我们自己保存：

```text
飞书 chat_id + user_id -> Claude session_id
```

### 3. 默认打断

同一个飞书会话中，Claude 正在执行时用户又发了一条普通消息：

```text
自动停止旧任务，再执行新消息。
```

```python
await client.interrupt()

# 旧任务必须继续读到结束
async for item in client.receive_response():
    ...

await client.query(new_text)
async for item in client.receive_response():
    ...
```

`/stop` 只执行：

```python
await client.interrupt()
```

不启动新任务。

### 4. 流式回复

```python
ClaudeAgentOptions(include_partial_messages=True)
```

在 `receive_response()` 中收到 `StreamEvent`，提取文本增量后更新飞书消息或卡片。飞书侧明确使用旧版 interactive `message.patch`，不使用 CardKit JSON 2.0（V2）、卡片实体或 V2 专用交互结构。

### 5. 工具权限卡片

```python
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny


async def can_use_tool(tool_name, tool_input, context):
    # 发飞书卡片，等待用户点击
    allowed = await wait_for_feishu_click()

    if allowed:
        return PermissionResultAllow(updated_input=tool_input)
    return PermissionResultDeny(message="用户拒绝")


options = ClaudeAgentOptions(
    cwd=workspace,
    permission_mode="default",
    can_use_tool=can_use_tool,
)
```

飞书卡片对应：

```text
Claude 请求工具
-> can_use_tool()
-> 飞书：允许一次 / 本会话允许 / 拒绝
-> 返回 Allow 或 Deny
-> Claude 继续或停止
```

### 6. 权限模式

| 模式 | 用途 |
|---|---|
| `default` | 默认，需确认的工具交给飞书卡片 |
| `plan` | 只规划/分析，不执行工具 |
| `acceptEdits` | 自动接受编辑操作 |
| `dontAsk` | 不弹授权卡片；未预授权的工具直接拒绝 |
| `bypassPermissions` | 全跳过权限并自动执行工具；不要给飞书普通用户 |

飞书首版默认：

```python
permission_mode="default"
```

发送 `/mode dontAsk` 后不再弹授权卡片，但没有预授权的工具会直接被拒绝；如果需要完全自动执行工具，只能由受信任的管理员配置 `bypassPermissions`，不要向普通聊天用户开放。

---

## SDK 能用什么

| 功能 | 是否首版需要 |
|---|---|
| 发消息、收 Claude 回复 | 必须 |
| 流式文本 | 必须 |
| 多轮会话 | 必须 |
| session 恢复 | 必须 |
| 中断任务 | 必须 |
| 工具权限回调 | 必须 |
| `cwd` + 自动读取 `CLAUDE.md` | 必须 |
| MCP | 暂不需要 |
| 子 Agent | 暂不需要 |
| Plugins / Skills | 暂不需要 |
| `env` | 暂不需要 |
| `max_turns` | 后续防无限循环再加 |
| `max_budget_usd` | 后续确认计费后再加 |
| 文件回退 checkpoint | 暂不需要 |

---

## 飞书版只需要的流程

```text
飞书消息
  -> client.query(text)
  -> receive_response()
  -> 飞书回复 / 更新卡片

工具需要授权
  -> can_use_tool()
  -> 飞书按钮
  -> PermissionResultAllow / PermissionResultDeny

新消息到达且旧任务正在跑
  -> interrupt()
  -> 读完旧任务结束事件
  -> query(新消息)

服务重启
  -> SQLite 取 session_id
  -> ClaudeAgentOptions(resume=session_id)
```
