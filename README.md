<div align="center">
  <h1>nano-alice: 超轻量个人 AI 助手</h1>
  <p>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

> **声明**：本项目基于 [nanobot](https://github.com/HKUDS/nanobot)（MIT 协议），自 2026-02-22 起剥离原项目独立开发维护。

> [!WARNING]
> **当前分支 `experiment` 为实验性分支。**
>
> 本分支的目标是探索 **Agent 自主持续迭代**：在一个基础框架之上，让 Agent 7x24 小时自主运行，
> 并赋予其观察自身运行日志、理解自身工作方式、进而修改自身代码与行为的能力。
>
> 本质上，这是对 Agent 权限的进一步放大——从「被动执行指令」走向「主动观察、反思与自我改进」。
>
> 但无约束的自我修改只会走向混乱，毫无意义。因此本分支同时建立一套规则与边界，确保迭代在可控的框架内进行：
> - Agent 可以读取自身的运行日志，了解自己是如何工作的
> - Agent 可以在规则允许的范围内修改自身的代码与配置
> - 所有变更必须遵循既定的约束条件，避免无方向的漫游
> - 目标是 **有纪律的自主进化**，而非不可预测的失控

## 安装

**从源码安装**（推荐用于开发）

```bash
git clone https://github.com/arcaneorion/nano-alice.git
cd nano-alice
pip install -e .
```

## 快速开始

> [!TIP]
> 在 `~/.nano-alice/config.json` 中设置 API Key。
> 获取 API Key：[OpenRouter](https://openrouter.ai/keys)（全球可用）· [Brave Search](https://brave.com/search/api/)（可选，用于网页搜索）

**1. 初始化**

```bash
nano-alice onboard
```

**2. 配置** (`~/.nano-alice/config.json`)

*设置 API Key*（以 OpenRouter 为例）：
```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  }
}
```

*设置模型*：
```json
{
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    }
  }
}
```

**3. 开始对话**

```bash
nano-alice agent
```

## 聊天平台

支持将 nano-alice 接入各种聊天平台：

| 平台 | 所需信息 |
|------|----------|
| **Telegram** | Bot token（从 @BotFather 获取） |
| **Discord** | Bot token + Message Content intent |
| **WhatsApp** | 扫码连接 |
| **飞书** | App ID + App Secret |
| **钉钉** | App Key + App Secret |
| **Slack** | Bot token + App-Level token |
| **Email** | IMAP/SMTP 凭据 |
| **QQ** | App ID + App Secret |

详细配置方式请参考原项目 [nanobot 文档](https://github.com/HKUDS/nanobot)。

## 配置

配置文件：`~/.nano-alice/config.json`

### Providers

| Provider | 用途 | 获取 API Key |
|----------|------|-------------|
| `custom` | 任意 OpenAI 兼容端点 | — |
| `openrouter` | LLM（推荐，可访问所有模型） | [openrouter.ai](https://openrouter.ai) |
| `anthropic` | LLM（Claude） | [console.anthropic.com](https://console.anthropic.com) |
| `openai` | LLM（GPT） | [platform.openai.com](https://platform.openai.com) |
| `deepseek` | LLM（DeepSeek） | [platform.deepseek.com](https://platform.deepseek.com) |
| `groq` | LLM + 语音转写（Whisper） | [console.groq.com](https://console.groq.com) |
| `gemini` | LLM（Gemini） | [aistudio.google.com](https://aistudio.google.com) |
| `volcengine` | LLM（火山引擎） | [volcengine.com](https://www.volcengine.com) |
| `dashscope` | LLM（通义千问） | [dashscope.console.aliyun.com](https://dashscope.console.aliyun.com) |
| `moonshot` | LLM（Kimi） | [platform.moonshot.cn](https://platform.moonshot.cn) |
| `zhipu` | LLM（智谱 GLM） | [open.bigmodel.cn](https://open.bigmodel.cn) |
| `vllm` | LLM（本地部署） | — |

### MCP (Model Context Protocol)

支持 [MCP](https://modelcontextprotocol.io/)，可连接外部工具服务器作为原生 Agent 工具使用。

```json
{
  "tools": {
    "mcpServers": {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
      },
      "my-remote-mcp": {
        "url": "https://example.com/mcp/",
        "headers": {
          "Authorization": "Bearer xxxxx"
        }
      }
    }
  }
}
```

### 安全

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `tools.restrictToWorkspace` | `false` | 设为 `true` 可将 Agent 工具限制在工作区目录内 |
| `channels.*.allowFrom` | `[]`（允许所有人） | 用户白名单 |

## CLI 参考

| 命令 | 说明 |
|------|------|
| `nano-alice onboard` | 初始化配置和工作区 |
| `nano-alice agent -m "..."` | 单次对话 |
| `nano-alice agent` | 交互式对话模式 |
| `nano-alice gateway` | 启动网关（接入聊天平台） |
| `nano-alice status` | 查看状态 |
| `nano-alice channels login` | 连接 WhatsApp（扫码） |
| `nano-alice channels status` | 查看平台连接状态 |

## 项目结构

```
nano_alice/
├── agent/          # 核心 Agent 逻辑
│   ├── loop.py     #   Agent 循环（LLM ↔ 工具执行）
│   ├── context.py  #   Prompt 构建
│   ├── memory.py   #   持久化记忆
│   ├── skills.py   #   技能加载
│   ├── subagent.py #   后台任务执行
│   └── tools/      #   内置工具
├── skills/         # 内置技能
├── channels/       # 聊天平台集成
├── bus/            # 消息路由
├── cron/           # 定时任务
├── heartbeat/      # 主动唤醒
├── providers/      # LLM 提供商
├── session/        # 会话管理
├── config/         # 配置
└── cli/            # 命令行
```

## Docker

```bash
docker compose run --rm nano-alice-cli onboard   # 首次初始化
vim ~/.nano-alice/config.json                     # 添加 API Key
docker compose up -d nano-alice-gateway           # 启动网关
```

## License

MIT
