# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 提供代码库指导。

## Git 提交规范

- **所有 commit message 必须使用中文**
- 格式：`类型(范围): 描述`，如 `feat(memory): 新增记忆子代理`、`fix(loop): 修复心跳触发记忆代理的问题`
- 类型：feat / fix / refactor / docs / test / chore

## 项目概述

nanobot 是一个超轻量个人 AI 助手框架（~4,000 行核心代码）。PyPI 包名 `nanobot-ai`，Python ≥3.11，构建后端 hatchling。

## 构建与开发命令

```bash
# 从源码安装（可编辑模式）
pip install -e .

# 安装开发依赖（pytest, pytest-asyncio, ruff）
pip install -e ".[dev]"

# 运行所有测试
pytest

# 运行单个测试文件
pytest tests/test_tool_validation.py

# Lint
ruff check nanobot/

# 格式检查
ruff format --check nanobot/

# 统计核心代码行数（不含 channels/, cli/, providers/）
bash core_agent_lines.sh
```

## 架构

系统采用**消息总线架构**，聊天频道与 agent 核心通过异步队列解耦。

### 数据流

```
Channel (Telegram/Discord/飞书等) → InboundMessage → MessageBus → AgentLoop → LLM
                                                                       ↓
Channel ← OutboundMessage ← MessageBus ← AgentLoop ← 工具执行循环
```

### 核心引擎

- **`nanobot/agent/loop.py`** — `AgentLoop`：骨架。`_run_agent_loop()` 是核心 while 循环：调用 LLM → 检查 tool_calls → 执行 → 重复，直到 `finish_reason != tool_calls` 或达到 `max_iterations`。`process_direct()` 用于 CLI/cron，`run()` 用于 gateway 模式——两者共用 `_process_message()` → `_run_agent_loop()`。
- **`nanobot/agent/context.py`** — `ContextBuilder`：从引导文件（AGENTS.md, SOUL.md, USER.md, TOOLS.md）、记忆、技能组装 system prompt。为 LLM 调用构建消息列表。

### 记忆系统（v2：被动式记忆管理 + RAG 注入）

记忆系统已改为**完全被动**，主 agent 不主动管理记忆（除非用户明确要求）。

#### 三个机制

1. **RAG 注入**（`loop.py` + `context.py`）
   - 每轮对话前，用 `_MemoryIndex` 对用户消息做 embedding 搜索（top_k=3）
   - 搜索结果注入到 system prompt 的 `# Recalled Context` 段（位于 Memory 之后、Skills 之前）
   - `_memory_index` 在 `AgentLoop.__init__` 中创建，复用 `memory_search.py` 的 `_MemoryIndex` 类
   - 出错不影响主流程（try/except 包裹）

2. **Memory Subagent**（`nanobot/agent/memory_agent.py`）
   - 每轮对话后 `asyncio.create_task` 后台运行，从最近 5 轮（10 条消息）提取信息
   - 拥有独立的 LLM provider（可配置不同模型和 API 端点）
   - 工具集：`read_file`, `write_file`, `edit_file`, `append_file`, `list_dir`, `memory_search`
   - 最多 10 轮 tool iteration，出错只 log 不影响主 agent
   - **不触发场景**：system channel 消息、heartbeat 消息
   - CLI 单消息模式通过 `await_pending()` 确保 subagent 完成后再退出

3. **纯裁剪 Consolidate**（`nanobot/agent/memory.py`）
   - `consolidate()` 已简化为纯标记更新（设置 `session.last_consolidated`），不再调用 LLM
   - 超过 `memory_window` 时触发，保留最近一半消息

#### 记忆文件结构

| 文件 | 用途 | 被索引? |
|------|------|---------|
| `memory/MEMORY.md` | 长期事实、偏好 | ✅ |
| `memory/HISTORY.md` | 追加式事件日志 | ✅ |
| `memory/SCRATCH.md` | 每轮对话概要（subagent 写入） | ❌（排除） |
| `memory/projects.md` | 活跃项目状态 | ✅ |
| `memory/lessons.md` | 经验教训 | ✅ |
| `memory/YYYY-MM-DD.md` | 每日日志 | ✅ |

#### Memory Subagent 配置

```json
{
  "agents": {
    "memory": {
      "enabled": true,
      "model": "gpt-5.2",
      "apiKey": "sk-xxx",
      "apiBase": "https://example.com/v1"
    }
  }
}
```

- `model` 为空 → 复用主 agent 的 provider 和模型
- `apiKey` + `apiBase` 有值 → 创建独立 `CustomProvider`（OpenAI 兼容）
- 只有 `model` 有值 → 通过 `_make_provider(config, model)` 自动匹配 providers 中的凭证

对应代码路径：
- 配置类：`nanobot/config/schema.py` → `MemoryAgentConfig`（嵌套在 `AgentsConfig.memory`）
- Provider 创建：`nanobot/cli/commands.py` → `_make_memory_provider()`
- 传入 AgentLoop：`memory_agent_config` + `memory_provider` 参数

### 工具

- **`nanobot/agent/tools/base.py`** — `Tool` ABC：所有工具实现 `name`, `description`, `parameters`（JSON Schema）和 `execute()`。内置参数校验 `validate_params()`。
- **`nanobot/agent/tools/registry.py`** — `ToolRegistry`：动态工具注册和执行。通过 `to_schema()` 生成 OpenAI 格式工具定义。
- **`nanobot/agent/tools/memory_search.py`** — `MemorySearchTool` + `_MemoryIndex`：基于 embedding 的语义搜索。`_MemoryIndex` 同时被 RAG 注入和 memory subagent 复用。索引时排除 `SCRATCH.md`。
- 内置工具：`filesystem.py`（read/write/edit/list）, `shell.py`（exec）, `web.py`（search/fetch）, `message.py`（发送到频道）, `spawn.py`（后台子 agent）, `cron.py`（定时任务）, `mcp.py`（MCP 集成）。

**添加新工具：**
1. 创建继承 `Tool` 的类，实现 `name`, `description`, `parameters`（JSON Schema dict）和 `async execute(**kwargs) -> str`。
2. 在 `AgentLoop._register_default_tools()` 中注册。
3. 需要运行时上下文的工具（channel, chat_id）实现 `set_context()` 并在 `AgentLoop._set_tool_context()` 中调用。

### 消息总线与事件

- **`nanobot/bus/queue.py`** — `MessageBus`：两个 `asyncio.Queue`（inbound/outbound）解耦频道与 agent。
- **`nanobot/bus/events.py`** — `InboundMessage`/`OutboundMessage` 数据类。`InboundMessage.session_key` = `channel:chat_id`。
- 进度流使用 `OutboundMessage`，`metadata["_progress"] = True`。
- 子 agent 通过 `InboundMessage`（`channel="system"`）公告结果，主循环作为注入消息处理。

### 频道

- **`nanobot/channels/base.py`** — `BaseChannel` ABC：频道实现 `start()`, `stop()`, `send()`。`_handle_message()` 检查 `allowFrom` 权限并发布到总线。
- **`nanobot/channels/manager.py`** — `ChannelManager`：从配置初始化已启用的频道，路由出站消息。

**添加新频道：** 创建继承 `BaseChannel` 的频道类，在 `schema.py` 中添加配置类和 `ChannelsConfig` 字段，在 `ChannelManager._init_channels()` 中添加初始化块（频道不自动发现）。

### Providers

- **`nanobot/providers/registry.py`** — `ProviderSpec` 数据类 + `PROVIDERS` 元组：LLM provider 元数据的唯一真相源。
- **`nanobot/providers/litellm_provider.py`** — 通过 LiteLLM 路由 LLM 调用。处理模型前缀、环境变量设置和 per-model 覆写。
- **`nanobot/providers/custom_provider.py`** — 直接 OpenAI 兼容 API（绕过 LiteLLM）。
- Provider 匹配优先级：模型名显式前缀 → 关键词匹配（PROVIDERS 元组顺序） → gateway 回退（第一个有 API key 的 gateway）。

**添加新 provider** 只需 2 步：在 `registry.py` 的 `PROVIDERS` 中添加 `ProviderSpec`，在 `schema.py` 中添加 `ProvidersConfig` 字段。其余（env vars, 前缀, 状态显示）自动工作。

### 会话与记忆

- **`nanobot/session/manager.py`** — `SessionManager`/`Session`：按 `channel:chat_id` 键的每会话历史。消息存储为 JSONL（追加模式，便于 LLM cache）。第一行是元数据，后续行是消息。
- **`nanobot/agent/memory.py`** — `MemoryStore`：两层记忆（MEMORY.md 长期事实 + HISTORY.md grep 可搜日志）。`consolidate()` 为纯裁剪，不调用 LLM。
- **`nanobot/agent/memory_agent.py`** — `MemoryAgent`：后台记忆子代理，每轮对话后提取信息写入记忆文件。

### 技能与子代理

- **`nanobot/agent/skills.py`** — `SkillsLoader`：从 workspace（`skills/`）和内置（`nanobot/skills/`）加载 SKILL.md。workspace 技能按名称覆盖内置。frontmatter 中 `always: true` 的技能嵌入每次 system prompt；其余以 XML 摘要展示，按需加载。
- **`nanobot/agent/subagent.py`** — `SubagentManager`：生成具有隔离上下文的后台 agent。

### 配置

- **`nanobot/config/schema.py`** — Pydantic 模型。`Config` 是根，继承 `BaseSettings`。环境变量覆盖使用 `NANOBOT_` 前缀，`__` 作为嵌套分隔符（如 `NANOBOT_AGENTS__DEFAULTS__MODEL`）。
- **`nanobot/config/loader.py`** — 加载/保存 `~/.nanobot/config.json`。
- 配置 JSON 使用 camelCase；Pydantic 模型使用 snake_case，通过 `alias_generator=to_camel` 自动转换。
- Workspace 默认 `~/.nanobot/workspace/`。包含引导文件（AGENTS.md, SOUL.md, USER.md）、`memory/`、`skills/`。
- `MemoryAgentConfig` 嵌套在 `AgentsConfig.memory` 下，支持 `enabled`, `model`, `api_key`, `api_base` 字段。

### 两种入口模式

1. **`nanobot agent`** — 直接 CLI 交互。创建 `AgentLoop`，调用 `process_direct()`。退出前调用 `await_pending()` 等待后台任务（memory subagent 等）完成。
2. **`nanobot gateway`** — 长运行服务器。并发启动 `AgentLoop.run()`、`ChannelManager`、`CronService`、`HeartbeatService`。`HeartbeatService` 每 30 分钟唤醒 agent 读取 `HEARTBEAT.md`。

### Bridge

`bridge/` 是 WhatsApp 频道的 Node.js TypeScript 项目（基于 Baileys）。独立构建 `npm install && npm run build`。强制包含在 wheel 的 `nanobot/bridge` 中。

### MCP 集成

MCP 服务器在 `config.tools.mcp_servers` 中配置。两种传输：stdio（`command`/`args`/`env`）和 HTTP（`url`/`headers`）。连接是懒加载的（首条消息时）。每个 MCP 工具注册为 `mcp_{server_name}_{tool_name}` 的 `MCPToolWrapper`。`AsyncExitStack` 管理所有会话生命周期。

## 代码规范

- Ruff linter：行长度 100，目标 Python 3.11，规则 E/F/I/N/W（E501 忽略）
- 测试使用 pytest，`asyncio_mode = "auto"` — `async def test_*` 函数无需装饰器
- 测试 mock 文件系统路径，使用 `tmp_path` 做真实文件 I/O；测试中不调用 LLM
- 配置 JSON 使用 camelCase；Python 代码使用 snake_case — Pydantic 通过 `alias_generator` 处理转换
- 工具通过 `to_schema()` 暴露 OpenAI function-calling 格式
- 所有工具 `execute()` 方法是 async 的，返回字符串；错误作为字符串返回，不抛异常
- 工具 `execute()` 签名使用 `**kwargs` 吸收 LLM 的意外参数
- 斜杠命令（`/new`, `/help`）在 `_process_message()` 中拦截，不经过 LLM
