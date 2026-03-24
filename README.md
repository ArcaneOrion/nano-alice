# nano-alice 🐈(本分支已与主分支分叉，于3/24决定归档)

> 本项目基于 [nanobot](https://github.com/HKUDS/nanobot)（MIT 协议），自 2026-02-22 起脱离原项目独立开发维护，定位为轻量级个人 AI 助手框架。

## 项目概览

nano-alice 采用消息总线架构，将聊天频道、Agent 主循环和工具执行解耦。当前仓库已包含：

- 多频道接入：Telegram、Discord、WhatsApp、飞书、钉钉、Slack、Email、QQ、Mochat
- Agent 工具链：文件系统、Shell、Web 搜索/抓取、消息发送、Cron、MCP、语义记忆搜索、后台任务
- 持久记忆：`MEMORY.md`、`HISTORY.md`、`SCRATCH.md` 等工作区文件
- 后台能力：定时任务、心跳唤醒、记忆子代理、MCP 工具注册
- Provider 适配：标准直连、网关、OAuth 与自定义 OpenAI 兼容端点

## 架构

系统采用**消息总线架构**，聊天频道与 Agent 核心通过异步队列解耦：

```text
频道 (Telegram/Discord/飞书/…) → InboundMessage → MessageBus → AgentLoop → LLM
                                                                     ↓
频道 ← OutboundMessage ← MessageBus ← AgentLoop ← 工具执行循环
```

### 核心模块

| 模块 | 说明 |
|------|------|
| `nano_alice/agent/loop.py` | Agent 主循环：LLM 调用、tool calls 执行、结果回注 |
| `nano_alice/agent/context.py` | system prompt 组装（身份、用户、记忆、技能） |
| `nano_alice/agent/memory.py` | 工作区记忆文件读写与裁剪 |
| `nano_alice/agent/memory_agent.py` | 每轮对话后后台提取长期记忆 |
| `nano_alice/agent/task_state.py` | 任务状态持久化 |
| `nano_alice/agent/tools/` | 内置工具：文件、Shell、Web、消息、Cron、MCP、RAG、spawn |
| `nano_alice/bus/` | Inbound / Outbound 事件与队列 |
| `nano_alice/channels/` | 各聊天频道适配器 |
| `nano_alice/providers/` | Provider registry 与实现（LiteLLM / OAuth / direct） |
| `nano_alice/session/` | 会话管理（按 `channel:chat_id` 隔离） |
| `nano_alice/cron/` | 定时任务存储与调度 |
| `nano_alice/heartbeat/` | 心跳服务，定期唤醒 agent |
| `nano_alice/config/` | 配置 schema 与加载逻辑 |
| `nano_alice/cli/commands.py` | CLI 入口与命令定义 |

### 目录结构

```text
nano-alice/
├── nano_alice/      # Python 主代码
├── tests/           # pytest 测试
├── bridge/          # WhatsApp Node.js + TypeScript bridge
├── workspace/       # 本地示例工作区
├── case/            # 演示素材
├── README.md
├── AGENTS.md
└── pyproject.toml
```

## 上下文与记忆架构

nano-alice 在每轮对话时，不是只把当前用户输入发给模型，而是把多层上下文一起组装后再调用 LLM。整体可分为四层：

1. **System Context**
   - 由 `AGENTS.md`、`SOUL.md`、`USER.md`、`IDENTITY.md`、长期记忆和任务状态共同组成
   - 作用：定义 agent 身份、行为约束、长期背景和当前任务规则

2. **Session History**
   - 来自当前 session 的最近对话消息与工具回放
   - 作用：让 agent 知道这段对话里刚刚发生了什么
   - 特点：是短期上下文，不等于长期记忆，也不等于任务状态

3. **Memory**
   - 来自 `MEMORY.md`、`HISTORY.md`、`SCRATCH.md`、`projects.md` 等工作区文件
   - 作用：保存跨轮、跨会话仍然重要的事实、偏好、项目信息和事件摘要
   - 机制：
     - 对话前可通过 RAG 检索相关记忆注入 prompt
     - 对话后由记忆子代理异步提取并写入记忆文件

4. **Task State**
   - 仅在多步任务场景启用
   - 作用：保存当前活跃任务的 plan、当前步骤、执行进度、result/evidence
   - 特点：只注入当前 active task，不注入历史任务，避免污染上下文

每轮对话时的大致流程如下：

```text
用户输入
  ↓
读取当前 session history
  ↓
检索相关 memory（RAG）
  ↓
读取当前 active task state（如存在）
  ↓
ContextBuilder 组装 system prompt + history + current context
  ↓
AgentLoop 调用 LLM / 执行工具 / 回写结果
  ↓
后台记忆子代理提取长期记忆
```

职责边界：

- `history`：我刚刚说了什么、做了什么
- `memory`：有哪些长期应该记住的事实
- `task_state`：当前任务做到哪一步了
- `system context`：我是谁、应遵循什么规则

## 记忆系统

记忆文件默认位于 `~/.nano-alice/workspace/memory/`：

| 文件 | 用途 |
|------|------|
| `MEMORY.md` | 核心事实与偏好，每轮全量注入 system prompt |
| `HISTORY.md` | 追加式事件日志 |
| `SCRATCH.md` | 最近对话摘要 |
| `schedule.md` | 作息、课表或日程信息 |
| `projects.md` | 活跃项目状态 |
| `lessons.md` | 经验教训 |
| `YYYY-MM-DD.md` | 每日日志 |

记忆相关行为：

1. **RAG 注入**：对话前检索相关记忆，注入 prompt
2. **记忆子代理**：对话后后台提取事实并写入记忆文件
3. **Consolidate 裁剪**：上下文超窗时保留最近一半消息

## 任务状态机制

为减少 agent “嘴上说做了、实际上没做”的情况，项目现在增加了当前任务状态层，和 `history` / `memory` 分工如下：

- `history`：保存原始对话与工具回放
- `memory`：保存长期事实、偏好和项目摘要
- `task_state`：保存**当前会话中唯一活跃任务**的计划与执行状态

任务状态默认存放在 `~/.nano-alice/workspace/task_state/`：

- `active/`：当前活跃任务
- `archive/`：已完成、失败或取消的历史任务

当 agent 自动识别到多步执行请求时，会进入 `task` 模式，并遵循以下规则：

1. **先计划，后执行**：进入 `task` 模式后必须先生成或确认 plan
2. **单步推进**：执行阶段只推进当前步骤，不跳步
3. **完成需有依据**：只有步骤存在 `result` 或 `evidence` 时才标记为 `done`
4. **计划失效可重排**：若原计划不成立，会进入 `replanning` 后再继续执行

当前版本的限制：

- 每个 session 同时只维护一个 active task
- 第一版仅支持线性 plan，不支持并行步骤或子任务树
- 只有当前 active task 会注入 system prompt；归档任务不会继续注入上下文

## 安装

### Python

```bash
git clone https://github.com/ArcaneOrion/nano-alice.git
cd nano-alice
pip install -e .
```

开发环境可选安装：

```bash
pip install -e .[dev]
```

要求：

- Python >= 3.11
- WhatsApp bridge 需要 Node.js >= 18 与 `npm`

### WhatsApp Bridge

仓库自带 `bridge/`，也可直接通过 CLI 首次登录时自动复制到 `~/.nano-alice/bridge` 并构建。

手动构建：

```bash
cd bridge
npm install
npm run build
```

## 配置

- 配置文件：`~/.nano-alice/config.json`
- 工作区：`~/.nano-alice/workspace/`
- 日志：`~/.nano-alice/logs/nano-alice.log`

初始化默认配置与工作区：

```bash
nano-alice onboard
```

首次执行会创建：

- `AGENTS.md`
- `SOUL.md`
- `USER.md`
- `IDENTITY.md`
- `memory/MEMORY.md`
- `memory/HISTORY.md`
- `skills/`
- `task_state/active/`
- `task_state/archive/`

### 基础配置示例

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "workspace": "~/.nano-alice/workspace",
      "model": "anthropic/claude-opus-4-5",
      "maxTokens": 8192,
      "temperature": 0.7,
      "maxToolIterations": 20,
      "memoryWindow": 50
    },
    "memory": {
      "enabled": true,
      "model": "",
      "apiKey": "",
      "apiBase": ""
    }
  },
  "heartbeat": {
    "enabled": true,
    "intervalS": 1800,
    "notifyChannel": "",
    "notifyChatId": ""
  },
  "tools": {
    "restrictToWorkspace": false,
    "web": {
      "search": {
        "apiKey": "",
        "tavilyApiKey": "",
        "maxResults": 5
      }
    },
    "exec": {
      "timeout": 60
    },
    "embeddings": {
      "apiBase": "",
      "apiKey": "",
      "model": "",
      "dimensions": 0,
      "extraHeaders": {},
      "ragMinScore": 0.35
    },
    "mcpServers": {}
  }
}
```

### 环境变量覆盖

配置支持 `NANO_ALICE_*` 前缀与 `__` 嵌套：

```bash
export NANO_ALICE_AGENTS__DEFAULTS__MODEL="deepseek/deepseek-chat"
```

## Provider 支持

完整 provider 清单以 `nano_alice/providers/registry.py` 为准：

| Provider | 类型 | 说明 |
|----------|------|------|
| `openrouter` | Gateway | 推荐，支持多模型路由 |
| `aihubmix` | Gateway | OpenAI 兼容网关 |
| `wanqing` | Gateway | 快手万擎 |
| `siliconflow` | Gateway | 硅基流动 |
| `volcengine` | Gateway | 火山引擎 |
| `anthropic` | Standard | Claude 直连 |
| `openai` | Standard | GPT 直连 |
| `deepseek` | Standard | DeepSeek 直连 |
| `gemini` | Standard | Gemini 直连 |
| `zhipu` | Standard | 智谱 GLM |
| `dashscope` | Standard | 阿里云通义千问 |
| `moonshot` | Standard | Moonshot / Kimi |
| `minimax` | Standard | MiniMax |
| `groq` | Standard | Groq（含转写能力） |
| `vllm` | Local | 本地 OpenAI 兼容服务 |
| `custom` | Direct | 任意 OpenAI 兼容直连端点 |
| `openai_codex` | OAuth | OpenAI Codex 登录 |
| `github_copilot` | OAuth | GitHub Copilot 登录 |

### OAuth 登录

```bash
nano-alice provider login openai-codex
nano-alice provider login github-copilot
```

## 频道支持

- Telegram
- Discord
- WhatsApp（需要 bridge）
- 飞书 / Lark
- 钉钉
- Slack
- Email（IMAP + SMTP）
- QQ
- Mochat

Telegram 配置示例：

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"],
      "replyToMessage": false
    }
  }
}
```

WhatsApp 登录：

```bash
nano-alice channels login
```

查看所有频道状态：

```bash
nano-alice channels status
```

## MCP 集成

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
        "headers": {
          "Authorization": "Bearer xxxxx"
        }
      }
    }
  }
}
```

注册后工具会以 `mcp_{server_name}_{tool_name}` 的形式暴露给 agent。

## CLI 使用

### 基础命令

```bash
# 查看版本
nano-alice --version

# 初始化配置和工作区
nano-alice onboard

# 单条消息
nano-alice agent -m "你好"

# 交互模式
nano-alice agent

# 指定会话 ID
nano-alice agent -s cli:demo -m "继续刚才的话题"

# 打开控制台日志
nano-alice agent --logs

# 查看状态
nano-alice status
```

### Gateway

```bash
# 启动多频道网关
nano-alice gateway

# 指定端口并输出详细日志
nano-alice gateway --port 18790 --verbose
```

启动后会连接已启用频道，并启动 cron 与 heartbeat 服务。

### 定时任务

```bash
# 查看任务
nano-alice cron list
nano-alice cron list --all

# 每 10 分钟执行一次
nano-alice cron add --name "巡检" --message "总结当前状态" --every 600

# 使用 cron 表达式
nano-alice cron add --name "早安" --message "总结今天日程" --cron "0 8 * * *" --tz "Asia/Shanghai"

# 指定一次性执行时间
nano-alice cron add --name "提醒" --message "开会前提醒我" --at "2026-03-06T18:00:00"

# 指定投递目标
nano-alice cron add --name "日报" --message "发送今日总结" --cron "0 18 * * *" --channel telegram --to 123456

# 启用 / 禁用
nano-alice cron enable <job_id>
nano-alice cron enable <job_id> --disable

# 手动执行
nano-alice cron run <job_id>
nano-alice cron run <job_id> --force

# 删除
nano-alice cron remove <job_id>
```

## 开发与测试

```bash
# 运行测试
pytest

# 静态检查
ruff check nano_alice/

# 格式检查
ruff format --check nano_alice/

# 格式化
ruff format nano_alice/
```

测试约定：

- 使用 `pytest`，`asyncio_mode = auto`
- 测试文件命名为 `tests/test_*.py`
- 尽量使用 `mock` / `tmp_path`，避免真实网络与真实 LLM 依赖

## 日志

- 文件日志默认写入 `~/.nano-alice/logs/nano-alice.log`
- 日志按 10MB 轮转，保留 7 天并 gzip 压缩
- `agent --logs` 和 `gateway --verbose` 控制控制台输出，不影响文件日志

## 安全说明

- 不要提交 `~/.nano-alice/` 下的密钥、聊天记录或个人数据
- 敏感配置建议通过 `NANO_ALICE_*` 环境变量覆盖
- 若启用 `tools.restrictToWorkspace=true`，工具访问会限制在工作区内

## 许可证

MIT — 详见 `LICENSE`
