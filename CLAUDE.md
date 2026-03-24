# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在本仓库中工作时提供指引。

## 构建与开发

```bash
pip install -e .              # 以可编辑模式安装
pip install -e '.[dev]'       # 包含开发依赖（pytest, ruff）
```

## 常用命令

```bash
nano-alice onboard            # 初始化配置和工作区
nano-alice agent              # 交互式 CLI 对话
nano-alice agent -m "hello"   # 单条消息模式
nano-alice gateway            # 启动网关（频道 + 定时任务 + 心跳）
nano-alice status             # 查看配置/Provider 状态
nano-alice channels status    # 查看频道连接状态
nano-alice cron list          # 列出定时任务
```

## 测试与代码检查

```bash
pytest                        # 运行所有测试
pytest tests/test_cron_service.py  # 运行单个测试文件
pytest -k "test_name"         # 运行指定测试
ruff check nano_alice/        # 代码检查
ruff format nano_alice/       # 代码格式化
```

Ruff 配置：行宽 100，目标 Python 3.11，规则 E/F/I/N/W，忽略 E501。
pytest 使用 `asyncio_mode = "auto"`。

## 架构

### 双模式消息流

系统采用双模式架构，解耦用户对话与内部维护：

```
┌─────────────────────────────────────┐
│            Agent Core               │
├─────────────────────────────────────┤
│                                     │
├──────────────┬──────────────────────┤
│ MessageBus   │   SignalBus          │
│ (Chat Mode)  │   (Reflect Mode)     │
├──────────────┼──────────────────────┤
│ 用户消息      │   内部信号            │
│ 对话历史      │   系统状态            │
└──────────────┴──────────────────────┘
```

**Chat Mode（对话模式）**：
```
Channel (Telegram/Slack/...) --> InboundMessage --> MessageBus --> AgentLoop
AgentLoop --> OutboundMessage --> MessageBus --> ChannelManager --> Channel.send()
```
- `bus/events.py`：`InboundMessage` / `OutboundMessage` 数据类。会话标识 = `"{channel}:{chat_id}"`。
- `bus/queue.py`：`MessageBus`，提供 `publish_inbound/outbound` 和 `consume_inbound/outbound`。

**Reflect Mode（反思模式）**：
```
Scheduler/TODO --> Signal --> SignalBus --> ReflectProcessor
```
- `agent/signals/`：内部信号系统（`SignalBus`、`Signal`、`AgentSignal` 枚举）。
- `agent/reflect/`：反思处理器，处理定时任务、待办检查等内部信号，不污染对话历史。

### Agent 核心 (`agent/`)

- **`loop.py` - AgentLoop**：核心引擎。同时运行两个循环：
  - `_chat_loop()`：处理用户消息（通过 MessageBus）
  - `_reflect_loop()`：处理内部信号（通过 SignalBus）
  关键方法：`_run_agent_loop()` 反复调用 LLM 直到没有更多工具调用或达到最大迭代次数。
- **`context.py` - ContextBuilder**：从引导文件（`AGENTS.md`、`SOUL.md`、`USER.md`、`TOOLS.md`、`IDENTITY.md`）、记忆（`MEMORY.md`）和技能组装系统提示词。通过 base64 编码处理图片附件。
- **`memory.py` - MemoryStore**：持久化记忆存储在 `workspace/memory/MEMORY.md`（加载到上下文）和 `HISTORY.md`（可 grep 搜索的日志）。整合功能通过 LLM 总结旧消息。
- **`skills.py` - SkillsLoader**：从 `nano_alice/skills/`（内置）和 `workspace/skills/`（用户自定义）加载技能。frontmatter 中标记 `always=true` 的技能始终加载；其余仅展示摘要，按需通过 `read_file` 加载。
- **`subagent.py`**：通过 `spawn` 工具执行后台任务。
- **`signals/` - 信号系统**：
  - `types.py`：`Signal` 数据类、`AgentSignal` 枚举（SCHEDULE_TRIGGER、TODO_CHECK 等）
  - `bus.py`：`SignalBus`，发布/订阅模式，等待所有 handler 完成并记录错误
- **`reflect/` - 反思处理器**：
  - `processor.py`：`ReflectProcessor`，处理内部信号，不污染对话历史
  - `internal_state.py`：`InternalState`，跟踪活动会话、系统健康状态

### 工具 (`agent/tools/`)

所有工具继承 `Tool`（`base.py` 中的 ABC），需实现 `name`、`description`、`parameters`（JSON Schema）和异步 `execute()`。通过 `ToolRegistry` 注册，由其负责参数验证和执行。

内置工具：`read_file`、`write_file`、`edit_file`、`list_dir`、`exec`（Shell）、`web_search`、`web_fetch`、`message`、`spawn`、`scheduler`。MCP 工具从配置中动态加载。

### 频道 (`channels/`)

所有频道继承 `BaseChannel`（`base.py` 中的 ABC），需实现 `start()`、`stop()`、`send()`。`ChannelManager` 根据配置初始化已启用的频道并分发出站消息。

支持：Telegram、Discord、WhatsApp（通过 `bridge/` 中的 Node.js 桥接）、飞书、钉钉、Slack、Email（IMAP/SMTP）、QQ、Mochat。

### Provider (`providers/`)

- **`base.py`**：`LLMProvider` ABC，`chat()` 返回 `LLMResponse`（content + tool_calls）。
- **`registry.py`**：`PROVIDERS` 元组包含 `ProviderSpec` — 所有 Provider 元数据的唯一数据源。新增 Provider：在此添加 `ProviderSpec` + 在 `config/schema.py` 中添加对应字段。
- **`litellm_provider.py`**：基于 LiteLLM 的主要 Provider 实现。处理模型前缀、环境变量、prompt 缓存。
- **`custom_provider.py`**：直接对接 OpenAI 兼容端点，绕过 LiteLLM。
- **`openai_codex_provider.py`**：基于 OAuth 的 OpenAI Codex Provider。

Provider 匹配优先级：显式模型前缀 > 关键词匹配 > 回退（第一个已配置的网关）。

### 配置 (`config/`)

- **`schema.py`**：Pydantic 模型。根 `Config` 类包含 `agents`、`channels`、`providers`、`gateway`、`tools` 等部分。同时接受 camelCase 和 snake_case 键名。
- **`loader.py`**：从 `~/.nano-alice/config.json` 加载配置。环境变量覆盖前缀：`NANO_ALICE_`。

### 其他服务

- **`scheduler/`**：定时任务调度（从 `cron/` 重命名）。支持 `every`（间隔）、`cron`（带时区）、`at`（一次性）三种调度方式。任务到期时通过 `SignalBus` 发出 `SCHEDULE_TRIGGER` 信号。任务存储在 `~/.nano-alice/cron/jobs.json`（路径保留以兼容旧数据）。
- **`todo/`**：待办事项服务（从 `heartbeat/` 重命名）。每 30 分钟检查一次 `workspace/TODO.md`（兼容 `HEARTBEAT.md`），有待办事项时发出 `TODO_CHECK` 信号。
- **`cron/`**：向后兼容适配器，导出 `CronService = SchedulerService`。
- **`heartbeat/`**：向后兼容适配器，导出 `HeartbeatService = TODOService`。
- **`session/`**：对话持久化，以 `"{channel}:{chat_id}"` 为键。

### 工作区 (`workspace/`)

引导文件加载到系统提示词：`AGENTS.md`（指令）、`SOUL.md`（人格）、`USER.md`（用户信息）、`TODO.md`（待办事项）、`IDENTITY.md`。记忆存储在 `memory/MEMORY.md` 和 `memory/HISTORY.md`。

**注意**：旧的 `HEARTBEAT.md` 文件名在过渡期仍然支持，但新代码使用 `TODO.md`。

## 关键约定

- **Commit 消息使用中文**：所有 commit message 必须使用中文书写。
- 配置 JSON 使用 **camelCase** 键名（Pydantic alias_generator 内部转换为 snake_case）。
- `bridge/` 目录是 TypeScript (Node.js) WhatsApp 桥接程序，需单独构建：`npm install && npm run build`。
- 项目从 `nanobot` 重命名为 `nano-alice`。技能 frontmatter 同时支持 `nanobot` 和 `openclaw` 元数据键以保持向后兼容。
- `experiment` 分支探索 Agent 自主迭代，在受控边界内进行。
