# nano-alice 🐈

> 本项目基于 [nanobot](https://github.com/HKUDS/nanobot)（MIT 协议），自 2026-02-22 起脱离原项目独立开发维护，仅供个人使用。

## 架构

系统采用**消息总线架构**，聊天频道与 agent 核心通过异步队列解耦：

```
频道 (Telegram/Discord/飞书/…) → InboundMessage → MessageBus → AgentLoop → LLM
                                                                     ↓
频道 ← OutboundMessage ← MessageBus ← AgentLoop ← 工具执行循环
```

### 核心模块

| 模块 | 说明 |
|------|------|
| `nano_alice/agent/loop.py` | Agent 主循环：调用 LLM → 检查 tool_calls → 执行 → 重复 |
| `nano_alice/agent/context.py` | System prompt 组装（引导文件 + 记忆 + 技能） |
| `nano_alice/agent/memory.py` | 两层记忆（MEMORY.md 长期事实 + HISTORY.md 事件日志） |
| `nano_alice/agent/memory_agent.py` | 记忆子代理，每轮对话后台提取信息写入记忆 |
| `nano_alice/agent/tools/` | 内置工具（文件、Shell、Web、消息、Cron、MCP 等；`read_file` 支持图片） |
| `nano_alice/agent/skills.py` | 技能加载器（内置 + workspace 自定义） |
| `nano_alice/bus/` | 消息总线（inbound/outbound 队列） |
| `nano_alice/channels/` | 聊天频道适配器 |
| `nano_alice/providers/` | LLM Provider 适配（LiteLLM / OpenAI 兼容 / OAuth） |
| `nano_alice/session/` | 会话管理（按 channel:chat_id 隔离，JSONL 存储） |
| `nano_alice/cron/` | 定时任务 |
| `nano_alice/heartbeat/` | 心跳服务（定期唤醒 agent 读取 HEARTBEAT.md） |

### 记忆系统

完全被动式记忆管理 + RAG 注入：

1. **RAG 注入** — 每轮对话前用 embedding 搜索相关记忆，注入 system prompt
2. **记忆子代理** — 每轮对话后后台运行，从最近对话中提取信息写入记忆文件
3. **纯裁剪 Consolidate** — 超过窗口时保留最近一半消息

记忆文件存储在 `~/.nano-alice/workspace/memory/` 下：

| 文件 | 用途 |
|------|------|
| `MEMORY.md` | 核心事实和偏好（每轮全量注入 system prompt） |
| `HISTORY.md` | 追加式事件日志 |
| `SCRATCH.md` | 每轮对话概要 |
| `schedule.md` | 课程表、作息时间 |
| `projects.md` | 活跃项目状态 |
| `lessons.md` | 经验教训 |
| `YYYY-MM-DD.md` | 每日日志 |

## 安装

```bash
git clone https://github.com/ArcaneOrion/nano-alice.git
cd nano-alice
pip install -e .
```

## 配置

配置文件：`~/.nano-alice/config.json`

### 初始化

```bash
nano-alice onboard
```

### 基础配置

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5",
      "maxTokens": 8192,
      "temperature": 0.7,
      "maxToolIterations": 20,
      "memoryWindow": 50
    }
  }
}
```

### 支持的 Provider

| Provider | 用途 | 获取 API Key |
|----------|------|-------------|
| `openrouter` | LLM（推荐，支持所有模型） | [openrouter.ai](https://openrouter.ai) |
| `anthropic` | Claude 直连 | [console.anthropic.com](https://console.anthropic.com) |
| `openai` | GPT 直连 | [platform.openai.com](https://platform.openai.com) |
| `deepseek` | DeepSeek 直连 | [platform.deepseek.com](https://platform.deepseek.com) |
| `gemini` | Gemini 直连 | [aistudio.google.com](https://aistudio.google.com) |
| `groq` | LLM + 语音转写 | [console.groq.com](https://console.groq.com) |
| `custom` | 任意 OpenAI 兼容端点 | — |
| `vllm` | 本地模型 | — |

完整列表见 `nano_alice/providers/registry.py`。

### 频道配置

支持 Telegram、Discord、WhatsApp、飞书、钉钉、Slack、Email、QQ、Mochat。

以 Telegram 为例：

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"]
    }
  }
}
```

### 记忆子代理配置

可为记忆子代理配置独立的模型和 API（使用更便宜的模型节省成本）：

```json
{
  "agents": {
    "memory": {
      "enabled": true,
      "model": "gpt-4.1-mini",
      "apiKey": "sk-xxx",
      "apiBase": "https://api.openai.com/v1"
    }
  }
}
```

### 心跳配置

心跳服务定期唤醒 agent 执行 `HEARTBEAT.md` 中的指令，可配置将结果发送到指定频道：

```json
{
  "heartbeat": {
    "enabled": true,
    "intervalS": 1800,
    "notifyChannel": "feishu",
    "notifyChatId": "ou_xxxxxx"
  }
}
```

- `notifyChannel` + `notifyChatId`：心跳结果自动发送到指定频道和用户（不配置则只执行不通知）
- 心跳通过 `MessageBus` 路由，使用独立会话，不影响用户对话

### MCP 集成

配置格式兼容 Claude Desktop / Cursor：

```json
{
  "tools": {
    "mcpServers": {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
      },
      "remote-mcp": {
        "url": "https://example.com/mcp/",
        "headers": { "Authorization": "Bearer xxxxx" }
      }
    }
  }
}
```

### 环境变量

支持通过环境变量覆盖配置，前缀 `NANO_ALICE_`，嵌套用 `__` 分隔：

```bash
export NANO_ALICE_AGENTS__DEFAULTS__MODEL="deepseek/deepseek-chat"
```

## 使用

### CLI 模式

```bash
# 单条消息
nano-alice agent -m "你好"

# 交互模式
nano-alice agent

# 显示运行日志
nano-alice agent --logs

# 查看状态
nano-alice status
```

### Gateway 模式（多频道服务）

```bash
nano-alice gateway
```

启动后自动连接所有已启用的频道，并运行定时任务和心跳服务。

### 日志

- 文件日志始终写入 `~/.nano-alice/logs/nano-alice.log`（10MB 轮转，保留 7 天，gzip 压缩）
- `--verbose`（gateway）/ `--logs`（agent）仅控制是否在控制台输出日志

### 定时任务

```bash
# 添加 cron 任务
nano-alice cron add --name "早安" --message "总结今天的日程" --cron "0 8 * * *"

# 列出任务
nano-alice cron list

# 手动执行
nano-alice cron run <job_id>

# 删除
nano-alice cron remove <job_id>
```

### CLI 命令一览

| 命令 | 说明 |
|------|------|
| `nano-alice onboard` | 初始化配置和工作区 |
| `nano-alice agent` | 交互式聊天 |
| `nano-alice agent -m "..."` | 单条消息 |
| `nano-alice gateway` | 启动多频道网关 |
| `nano-alice status` | 查看状态 |
| `nano-alice channels status` | 查看频道状态 |
| `nano-alice channels login` | WhatsApp 扫码登录 |
| `nano-alice provider login <name>` | OAuth 登录 |
| `nano-alice cron list` | 查看定时任务 |

## 项目结构

```
nano_alice/
├── agent/          # 核心 agent 逻辑
│   ├── loop.py     #   主循环（LLM ↔ 工具执行）
│   ├── context.py  #   Prompt 组装
│   ├── memory.py   #   持久记忆
│   ├── skills.py   #   技能加载
│   ├── subagent.py #   后台子代理
│   └── tools/      #   内置工具
├── skills/         # 内置技能（github, weather, tmux…）
├── channels/       # 聊天频道适配
├── bus/            # 消息总线
├── cron/           # 定时任务
├── heartbeat/      # 心跳唤醒
├── providers/      # LLM Provider
├── session/        # 会话管理
├── config/         # 配置
└── cli/            # CLI 命令
```

## 许可证

MIT — 详见 [LICENSE](LICENSE)
