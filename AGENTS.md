# Repository Guidelines / 仓库指南

## Project Overview / 项目概述

nano-alice is a lightweight personal AI assistant framework with a message bus architecture. It supports multiple chat channels (Telegram, Discord, WhatsApp, Feishu, DingTalk, Slack, Email, QQ, Mochat) and provides persistent memory, scheduled tasks, heartbeat services, and MCP integration.

nano-alice 是一个轻量级个人 AI 助手框架，采用消息总线架构。支持多聊天频道（Telegram、Discord、WhatsApp、飞书、钉钉、Slack、Email、QQ、Mochat），提供持久记忆、定时任务、心跳服务和 MCP 集成。

## Project Structure & Module Organization / 项目结构与模块组织

```
nano_alice/
├── agent/          # 核心 agent 逻辑
│   ├── loop.py     #   主循环（LLM ↔ 工具执行）
│   ├── context.py  #   Prompt 组装
│   ├── memory.py   #   持久记忆（MEMORY.md + HISTORY.md）
│   ├── memory_agent.py  # 记忆子代理（后台提取信息）
│   ├── skills.py   #   技能加载
│   ├── subagent.py #   后台子代理
│   └── tools/      #   内置工具
│       ├── base.py         # 工具基类
│       ├── filesystem.py   # 文件读写（支持图片）
│       ├── shell.py        # 命令执行
│       ├── web.py          # Web 搜索/抓取
│       ├── message.py      # 消息发送
│       ├── cron.py         # 定时任务
│       ├── mcp.py          # MCP 客户端
│       ├── memory_search.py # 语义记忆搜索
│       └── spawn.py        # 后台任务
├── skills/         # 内置技能（github, weather, tmux, cron, memory…）
├── channels/       # 聊天频道适配
├── bus/            # 消息总线（inbound/outbound 队列）
├── cron/           # 定时任务服务
├── heartbeat/      # 心跳唤醒服务
├── providers/      # LLM Provider 适配（LiteLLM / OpenAI 兼容 / OAuth）
├── session/        # 会话管理（按 channel:chat_id 隔离）
├── config/         # 配置加载与 schema
└── cli/            # CLI 命令
```

- `tests/`: pytest suite for CLI, cron, tools, and channel behaviors.
  `tests/`：CLI、定时任务、工具与频道行为测试。
- `bridge/`: Node.js + TypeScript WhatsApp bridge (Baileys).
  `bridge/`：Node.js + TypeScript WhatsApp 桥接模块（Baileys）。
- `workspace/`: Runtime prompts and memory files (local state).
  `workspace/`：运行时提示词与记忆文件（本地状态）。
- `case/`: Demo assets (GIF examples).
  `case/`：演示素材（GIF 示例）。

## Build, Test, and Development Commands / 构建、测试与开发命令

```bash
# 安装开发环境
pip install -e .

# 运行测试
pytest

# Python 静态检查
ruff check nano_alice/

# 格式检查（不改文件）
ruff format --check nano_alice/

# 格式化代码
ruff format nano_alice/

# 构建 WhatsApp 桥接
cd bridge && npm install && npm run build
```

## CLI Commands / CLI 命令

```bash
# 初始化配置和工作区
nano-alice onboard

# 交互式聊天
nano-alice agent

# 单条消息
nano-alice agent -m "你好"

# 显示运行日志
nano-alice agent --logs

# 启动多频道网关服务
nano-alice gateway

# 启动网关（显示详细日志）
nano-alice gateway --verbose

# 查看状态
nano-alice status

# 查看频道状态
nano-alice channels status

# WhatsApp 扫码登录
nano-alice channels login

# OAuth 登录 provider
nano-alice provider login <name>

# 定时任务管理
nano-alice cron list
nano-alice cron add --name "早安" --message "总结今天的日程" --cron "0 8 * * *"
nano-alice cron run <job_id>
nano-alice cron remove <job_id>
```

## Architecture / 架构

系统采用**消息总线架构**，聊天频道与 agent 核心通过异步队列解耦：

```
频道 (Telegram/Discord/飞书/…) → InboundMessage → MessageBus → AgentLoop → LLM
                                                                     ↓
频道 ← OutboundMessage ← MessageBus ← AgentLoop ← 工具执行循环
```

### Memory System / 记忆系统

- **RAG 注入** — 每轮对话前用 embedding 搜索相关记忆，注入 system prompt
- **记忆子代理** — 每轮对话后后台运行，从最近对话中提取信息写入记忆文件
- **纯裁剪 Consolidate** — 超过窗口时保留最近一半消息

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

当前实现中的记忆子代理采用**保守写入策略**：
- 自动重点维护 `MEMORY.md`、`HISTORY.md`、`SCRATCH.md`、`projects.md`、`lessons.md`
- `MEMORY.md` 仅用于长期稳定事实与偏好
- `projects.md` 优先承载项目状态更新，避免把项目流水写进 `MEMORY.md`
- `HISTORY.md` 记录关键事件、重要确认、失败与反转
- `YYYY-MM-DD.md` 当前不在默认自动写入路径内

## Supported Providers / 支持的 LLM Provider

| Provider | 类型 | 说明 |
|----------|------|------|
| `openrouter` | Gateway | 推荐，支持所有模型 |
| `aihubmix` | Gateway | OpenAI 兼容网关 |
| `wanqing` | Gateway | 快手万擎 |
| `siliconflow` | Gateway | 硅基流动 |
| `volcengine` | Gateway | 火山引擎 |
| `anthropic` | Standard | Claude 直连 |
| `openai` | Standard | GPT 直连 |
| `openai_codex` | OAuth | OpenAI Codex（OAuth 登录） |
| `github_copilot` | OAuth | GitHub Copilot（OAuth 登录） |
| `deepseek` | Standard | DeepSeek 直连 |
| `gemini` | Standard | Gemini 直连 |
| `zhipu` | Standard | 智谱 GLM |
| `dashscope` | Standard | 阿里云 Qwen |
| `moonshot` | Standard | Moonshot Kimi |
| `minimax` | Standard | MiniMax |
| `groq` | Standard | Groq（语音转写） |
| `vllm` | Local | 本地模型 |
| `custom` | Direct | 任意 OpenAI 兼容端点 |

完整列表见 `nano_alice/providers/registry.py`。

## Supported Channels / 支持的频道

- Telegram
- Discord
- WhatsApp（需要 Node.js 桥接）
- 飞书 / Lark
- 钉钉
- Slack
- Email（IMAP + SMTP）
- QQ
- Mochat

## MCP Integration / MCP 集成

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

MCP 工具会自动注册为 `mcp_{server_name}_{tool_name}` 格式。

## Configuration / 配置

- 配置文件：`~/.nano-alice/config.json`
- 工作区：`~/.nano-alice/workspace/`
- 日志：`~/.nano-alice/logs/nano-alice.log`（10MB 轮转，保留 7 天，gzip 压缩）
- 环境变量前缀：`NANO_ALICE_*`，嵌套用 `__` 分隔

```bash
export NANO_ALICE_AGENTS__DEFAULTS__MODEL="deepseek/deepseek-chat"
```

## Coding Style & Naming Conventions / 代码风格与命名规范

- Python: 4-space indentation, `snake_case` for functions/files, clear type hints when practical.
  Python：4 空格缩进，函数与文件使用 `snake_case`，尽量补充类型标注。
- Config JSON uses `camelCase` keys; Python models stay `snake_case`.
  配置 JSON 使用 `camelCase`，Python 模型保持 `snake_case`。
- Ruff rules apply (line length 100, E/F/I/N/W).
  使用 Ruff 规则（行宽 100，E/F/I/N/W）。
- Requires Python >= 3.11.

## Testing Guidelines / 测试规范

- Framework: `pytest` with asyncio auto mode.
  测试框架：`pytest`（asyncio auto）。
- Name files `tests/test_*.py` and tests `test_*`.
  文件命名 `tests/test_*.py`，函数命名 `test_*`。
- Prefer mocks and `tmp_path`; avoid real LLM/network dependencies in tests.
  优先使用 mock 与 `tmp_path`，避免真实 LLM/网络依赖。

## Commit & Pull Request Guidelines / 提交与 PR 规范

- Follow Conventional Commits style: `type(scope): description`.
  使用约定式提交：`type(scope): 描述`。
- Commit messages should be Chinese in this repo (for consistency with history).
  本仓库 commit 信息建议使用中文（与历史记录一致）。
- Common types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`.
  常用类型：`feat`、`fix`、`refactor`、`docs`、`test`、`chore`。
- PRs should include summary, linked issue, and test/lint evidence; add screenshots/log snippets for behavior changes.
  PR 需包含摘要、关联 issue、测试/静态检查结果；行为变更请附截图或日志片段。

## Security & Configuration Tips / 安全与配置提示

- Never commit API keys or personal data from `~/.nano-alice/`.
  禁止提交 `~/.nano-alice/` 下的密钥或个人数据。
- Use `NANO_ALICE_*` environment variables for sensitive overrides.
  敏感配置请通过 `NANO_ALICE_*` 环境变量覆盖。
