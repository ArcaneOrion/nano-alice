# 日志系统重构设计文档

> **状态**: 设计中
> **作者**: Claude + User
> **创建时间**: 2025-03-23
> **目标**: 打造一个渐进披露、Agent 易读的结构化日志系统

---

## 1. 背景

### 1.1 现状分析

当前 nano-alice 的日志系统采用 `loguru` 直接输出到 `stderr`：

```python
from loguru import logger
logger.info("Scheduler: executing job '{}'", job.name)
```

**存在的问题**：

| 问题 | 影响 |
|------|------|
| 无结构化存储 | Agent 无法读取自身运行状态 |
| 无持久化 | 重启即丢失，无法事后分析 |
| 无按组件过滤 | 所有日志混在一起 |
| 无时间窗口 | 无法查询"最近 30 分钟的错误" |
| 与状态系统脱节 | InternalState 无法感知日志指标 |

### 1.2 设计目标

```
┌─────────────────────────────────────────────────────┐
│                   日志系统目标                        │
├─────────────────────────────────────────────────────┤
│ 1. 结构化持久化  → JSONL 格式，按组件分文件           │
│ 2. Agent 易读    → logs 工具返回摘要，非原始数据       │
│ 3. 渐进披露      → CLI 按组件/级别/时间过滤           │
│ 4. 轻量简洁      → 惰性清理 6 小时过期日志            │
│ 5. 状态感知      → 与 InternalState 集成              │
└─────────────────────────────────────────────────────┘
```

---

## 2. 核心设计决策

### 2.1 设计决策表

| 设计点 | 选择 | 理由 |
|--------|------|------|
| 文件格式 | **JSONL** | 可追加，查询时过滤过期条目 |
| 组件划分 | **逻辑分组** | agent, channels, scheduler, signals, reflect, tools |
| 组件识别 | **从 logger 名称自动推断** | 无需到处加 `.bind(component="xxx")`，v1 粗粒度分组 |
| 日志目录 | **`~/.nano-alice/logs/`** | 与 sessions、cron 等运行态数据一致 |
| 轮转策略 | **写入时清理 + 写锁保护** | gateway 长期运行也需要清理，并发安全 |
| 日志字段 | **ts, level, component, event, msg, data** | 结构化但不过度 |
| CLI 输出 | **rich 表格** | 人类友好，类似 `nano-alice status` |
| 错误处理 | **警告（stderr）** | 不抛异常，不影响主流程 |
| entry point | **单例初始化** | 所有入口（含 logs 命令）共享同一配置 |
| sink 分离 | **console + file 独立 handler** | `logger.remove()` 后分别添加，console 控级别，file 始终写 |
| 静默控制台 | **console sink level 过滤** | 不用 `logger.disable()`，用 `logger.remove()` 重置后分别配置，避免影响 file sink |
| 信号推送 | **asyncio.create_task()** | 同步 write() 中安全推送异步信号 |
| 清理并发 | **每组件 asyncio.Lock** | 保护 _cleanup() 和 _append() 互斥 |
| 查询排序 | **先收集后排序再 limit** | 保证返回真正的最新 N 条 |

### 2.2 日志条目格式

```json
{"ts":"2025-03-23T10:30:45.123Z","level":"INFO","component":"scheduler","event":"job_executed","msg":"Executed job 'daily-report'","data":{"job_id":"abc","duration_ms":2300}}
```

字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| ts | string | ISO 8601 时间戳 |
| level | string | DEBUG/INFO/WARNING/ERROR |
| component | string | agent/channels/scheduler/signals/reflect/tools |
| event | string | 事件类型，如 job_executed/message_sent |
| msg | string | 人类可读消息 |
| data | object | 结构化附加数据 |

---

## 3. 架构设计

### 3.1 目录结构

```
~/.nano-alice/              # get_data_path()
├── logs/                   # 新增：日志目录
│   ├── agent.jsonl         # AgentLoop, ContextBuilder, Memory, Subagent
│   ├── channels.jsonl      # 所有渠道活动
│   ├── scheduler.jsonl     # Scheduler 服务
│   ├── signals.jsonl       # SignalBus
│   ├── reflect.jsonl       # ReflectProcessor
│   └── tools.jsonl         # 工具执行
├── sessions/               # 现有
├── cron/                   # 现有
└── config.json             # 现有

nano_alice/
├── log/
│   ├── __init__.py         # ensure_logging_initialized(), get_log_store(), _infer_component()
│   ├── types.py            # LogEntry, LogLevel, Component
│   └── store.py            # LogStore, _FileSink
├── utils/
│   └── helpers.py          # 新增 get_logs_path()
├── agent/
│   ├── tools/
│   │   └── logs.py         # LogsTool
│   ├── signals/
│   │   └── types.py        # 新增 AgentSignal.LOG_ERROR
│   └── reflect/
│       ├── processor.py    # 新增 _handle_log_error()
│       └── internal_state.py  # 扩展字段
└── cli/
    └── commands.py         # 新增 logs 子命令，各入口调用 ensure_logging_initialized()
```

### 3.2 数据流

```
┌─────────────────────────────────────────────────────────────┐
│                        应用层                                │
│  AgentLoop | Channels | Scheduler | Signals | Tools         │
└────────────────────────┬────────────────────────────────────┘
                         │ logger.info(...)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                      loguru                                  │
│  (自动从 record['name'] 推断 component)                      │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    _FileSink                                 │
│  (解析 record，构造 LogEntry)                                │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    LogStore                                 │
│  写入 JSONL + 每 ~100 次清理 + 推送 LOG_ERROR 信号            │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                  ~/.nano-alice/logs/*.jsonl                  │
└─────────────────────────────────────────────────────────────┘

                         │
                         ▼
┌──────────────────┬──────────────────┬──────────────────┐
│  CLI logs 命令    │  LogsTool       │  InternalState  │
│  (人类查询)       │  (Agent 查询)    │  (状态感知)       │
└──────────────────┴──────────────────┴──────────────────┘
```

---

## 4. 核心实现

### 4.1 组件自动推断

**问题**：现有代码没有 `.bind(component="xxx")`，改造成本高。

**方案**：从 logger 名称（`record['name']`）推断组件：

```python
# nano_alice/log/__init__.py

def _infer_component(record: dict) -> Component:
    """从 logger 名称推断组件

    映射规则：
    - nano_alice.agent*         → agent
    - nano_alice.channels*      → channels
    - nano_alice.scheduler*     → scheduler
    - nano_alice.cron*          → scheduler (兼容旧名)
    - nano_alice.agent.signals* → signals
    - nano_alice.agent.reflect* → reflect
    - nano_alice.agent.tools*   → tools
    """
    name = record.get("name", "")

    if name.startswith("nano_alice.channels"):
        return Component.CHANNELS
    elif name.startswith("nano_alice.scheduler") or name.startswith("nano_alice.cron"):
        return Component.SCHEDULER
    elif name.startswith("nano_alice.agent.signals"):
        return Component.SIGNALS
    elif name.startswith("nano_alice.agent.reflect"):
        return Component.REFLECT
    elif name.startswith("nano_alice.agent.tools"):
        return Component.TOOLS
    else:
        return Component.AGENT  # 默认
```

**优势**：现有 logger 调用**完全不需要修改**。

**注意**：v1 版本组件推断是粗粒度分组。部分模块（如 `session/manager.py`、`bus/*`、`todo/service.py`）会归入默认 `agent` 组件。如需精细分类，后续可扩展推断规则或添加 `.bind()`。

### 4.2 单例初始化

**问题**：gateway、agent、cron run 各自独立启动，容易漏初始化。

**方案**：单例模式 + idempotent：

```python
# nano_alice/log/__init__.py

_logging_initialized = False
_log_store: LogStore | None = None

def ensure_logging_initialized(retention_hours: int = 6) -> LogStore:
    """确保日志系统已初始化（idempotent）

    所有入口都应该调用这个函数：
    - gateway()
    - agent()
    - cron_run()
    - logs()  -- 查询命令也需要初始化

    返回 LogStore 实例，供其他模块使用。

    设计说明：
    - logger.remove() 移除 loguru 默认 handler，完全控制 sink 配置
    - 后续不再使用 logger.disable()，避免影响 file sink
    - console sink 通过 level 参数控制输出级别（默认 INFO）
    - file sink 始终写入，不受 console level 影响
    """
    global _logging_initialized, _log_store

    if _logging_initialized:
        return _log_store

    log_dir = get_logs_path()
    log_dir.mkdir(parents=True, exist_ok=True)

    _log_store = LogStore(log_dir, retention_hours)

    # 移除 loguru 默认 handler，重新配置
    logger.remove()

    # console sink：控制台输出，可通过 level 参数控制静默
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO",  # 可通过配置调整为 WARNING/SUCCESS 来静默
    )

    # file sink：结构化落盘，始终写入，不受 console level 影响
    logger.add(
        _FileSink(_log_store),
        filter=lambda r: r["name"].startswith("nano_alice"),
        serialize=False,
    )

    _logging_initialized = True
    return _log_store

def get_log_store() -> LogStore | None:
    """获取 LogStore 实例"""
    return _log_store
```

**入口集成**：

```python
# cli/commands.py

@app.command()
def gateway(...):
    ensure_logging_initialized()  # 在最开始
    # ... 其余代码

@app.command()
def agent(...):
    ensure_logging_initialized()
    # ... 其余代码

@app.command()
def cron_run(...):
    ensure_logging_initialized()
    # ... 其余代码

@app.command()
def logs(...):
    ensure_logging_initialized()  # 查询命令也需要先初始化
    # ... 其余代码
```

### 4.3 自定义 FileSink

**问题**：loguru 的 `logger.disable("nano_alice")` 在 logger 层级拦截，会影响所有 sink。

**方案**：彻底移除 `logger.disable()` 的使用，改用 sink 级别的控制：

| 控制方式 | 原设计 | 新方案 |
|----------|--------|--------|
| 静默控制台 | `logger.disable("nano_alice")` | console sink 的 `level` 参数 |
| 文件落盘 | 受 disable 影响 | 独立 sink，始终写入 |

**实现要点**：
1. `ensure_logging_initialized()` 中调用 `logger.remove()` 清空默认配置
2. console sink 用 `level="INFO"` 控制输出，需要静默时可改为 `level="WARNING"`
3. file sink 独立添加，不受 console level 影响

```python
# nano_alice/log/store.py

class _FileSink:
    """loguru 自定义 sink

    从 loguru record 提取结构化数据，写入 LogStore。
    注意：需要在初始化时移除 logger.disable("nano_alice")，
    改用 console sink 的 level 参数控制静默。
    """

    def __init__(self, store: LogStore):
        self._store = store

    def write(self, message: str) -> str:
        """loguru 回调

        注意：loguru 传递的是 formatted message string，
        但 message 对象有隐藏的 .record 属性。
        """
        # 获取原始 record（loguru 内部机制）
        record = message.record  # type: ignore

        # 构造 LogEntry
        entry = LogEntry(
            ts=datetime.fromtimestamp(record["time"].timestamp()),
            level=LogLevel(record["level"].name),
            component=_infer_component(record),
            event=record.get("extra", {}).get("event", "log"),
            msg=record["message"],
            data=record.get("extra", {}).get("data", {}),
        )

        self._store.write(entry)
        return message  # loguru 要求返回 message
```

### 4.4 LogStore 核心逻辑

```python
# nano_alice/log/store.py

class LogStore:
    """结构化日志存储

    特性：
    - 按 component 分文件存储
    - 写入时触发清理（每 ~100 次）
    - 每组件独立锁，保护并发写入/清理
    - 推送 LOG_ERROR 信号到 SignalBus（异步 fire-and-forget）
    """

    _cleanup_threshold = 100

    def __init__(self, log_dir: Path, retention_hours: int = 6):
        self._dir = log_dir
        self._retention = timedelta(hours=retention_hours)
        self._signal_bus: SignalBus | None = None
        self._cleanup_counters: dict[Component, int] = {
            c: 0 for c in Component
        }
        # 每组件一把锁，保护 _cleanup() 和 _append() 互斥
        self._locks: dict[Component, asyncio.Lock] = {
            c: asyncio.Lock() for c in Component
        }

    def set_signal_bus(self, bus: SignalBus) -> None:
        """设置信号总线，用于推送状态更新"""
        self._signal_bus = bus

    def write(self, entry: LogEntry) -> None:
        """写入日志 + 随机清理 + 推送信号"""
        # 1. 追加写入（带锁保护）
        lock = self._locks[entry.component]
        try:
            # 尝试获取当前线程的事件循环
            loop = asyncio.get_running_loop()
            # 如果在 async 上下文中，用同步方式等待锁
            # 这里简化处理：直接用文件锁或者 threading.Lock
            # 实际实现需要根据部署环境选择
        except RuntimeError:
            # 无 event loop，同步上下文
            pass

        # 简化实现：直接追加（清理时用锁）
        self._append(entry)

        # 2. 清理计数器
        self._cleanup_counters[entry.component] += 1
        if self._cleanup_counters[entry.component] >= self._cleanup_threshold:
            self._cleanup_counters[entry.component] = 0
            self._cleanup(entry.component)

        # 3. 推送错误信号（异步 fire-and-forget）
        if entry.level == LogLevel.ERROR and self._signal_bus:
            try:
                loop = asyncio.get_running_loop()
                # 创建后台任务，不阻塞写入
                loop.create_task(self._signal_bus.publish(Signal(
                    type=AgentSignal.LOG_ERROR,
                    data={
                        "component": entry.component.value,
                        "msg": entry.msg,
                        "ts": entry.ts.isoformat(),
                    },
                    source="log_store",
                )))
            except RuntimeError:
                # 无 event loop，跳过信号推送
                pass

    def _append(self, entry: LogEntry) -> None:
        """追加写入文件"""
        path = self._dir / f"{entry.component.value}.jsonl"
        with open(path, "a") as f:
            f.write(entry.to_jsonl() + "\n")

    def _cleanup(self, component: Component) -> None:
        """重写文件，移除过期条目（带锁保护）"""
        cutoff = datetime.now() - self._retention
        path = self._dir / f"{component.value}.jsonl"

        if not path.exists():
            return

        # 加锁保护，避免与 _append() 并发冲突
        # 简化实现：实际需要用 threading.Lock 或文件锁
        valid_lines = []
        for line in path.read_text().splitlines():
            entry = LogEntry.from_jsonl(line)
            if entry.ts > cutoff:
                valid_lines.append(line)

        # 原子替换：先写临时文件，再 rename
        temp_path = path.with_suffix(".jsonl.tmp")
        temp_path.write_text("\n".join(valid_lines) + ("\n" if valid_lines else ""))
        temp_path.replace(path)  # 原子操作

    def query(self,
              component: Component | None = None,
              level: LogLevel | None = None,
              since: datetime | None = None,
              limit: int = 100) -> list[LogEntry]:
        """查询日志

        注意：先收集所有符合条件的条目，排序后再应用 limit。
        这样保证返回的是真正的"最新 N 条"，而非"先扫描到的 N 条"。
        """
        results = []

        files = self._files_for_component(component)
        for path in files:
            for line in path.read_text().splitlines():
                entry = LogEntry.from_jsonl(line)

                # 过滤
                if level and entry.level != level:
                    continue
                if since and entry.ts < since:
                    continue

                results.append(entry)

        # 按 ts 倒序，然后应用 limit
        results.sort(key=lambda e: e.ts, reverse=True)
        return results[:limit]

    def summarize(self, component: Component | None = None) -> dict:
        """返回摘要（给 Agent 用）

        返回格式：
        {
            "total": 42,
            "errors": 0,
            "warnings": 2,
            "by_event": {"job_executed": 10, "message_sent": 32}
        }
        """
        entries = self.query(component, limit=10000)

        summary = {
            "total": len(entries),
            "errors": sum(1 for e in entries if e.level == LogLevel.ERROR),
            "warnings": sum(1 for e in entries if e.level == LogLevel.WARNING),
            "by_event": {},
        }

        for e in entries:
            summary["by_event"][e.event] = summary["by_event"].get(e.event, 0) + 1

        return summary
```

### 4.5 LogsTool（Agent 工具）

```python
# nano_alice/agent/tools/logs.py

class LogsTool(Tool):
    """日志查询工具"""

    name = "logs"
    description = (
        "查询系统日志，了解运行状态。"
        "可以按组件、级别、时间窗口过滤，返回摘要或原始日志。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "component": {
                "enum": ["agent", "channels", "scheduler", "signals", "reflect", "tools"],
                "description": "组件名称",
            },
            "level": {
                "enum": ["DEBUG", "INFO", "WARNING", "ERROR"],
                "description": "日志级别",
            },
            "last": {
                "type": "string",
                "description": "时间窗口，如 30m, 1h, 6h",
            },
            "summarize": {
                "type": "boolean",
                "description": "返回摘要而非原始日志",
            },
        },
    }

    async def execute(self,
                     component: str | None = None,
                     level: str | None = None,
                     last: str = "1h",
                     summarize: bool = True) -> str:
        """执行查询"""
        from nano_alice.log import get_log_store, Component, LogLevel

        store = get_log_store()
        if not store:
            return "日志系统未初始化"

        # 解析时间窗口
        since = datetime.now() - self._parse_duration(last)

        # 查询
        entries = store.query(
            component=Component(component) if component else None,
            level=LogLevel(level) if level else None,
            since=since,
            limit=100,
        )

        if summarize:
            return self._format_summary(entries, component, last)
        else:
            return self._format_entries(entries)

    def _parse_duration(self, duration: str) -> timedelta:
        """解析时间窗口字符串"""
        if duration.endswith("m"):
            return timedelta(minutes=int(duration[:-1]))
        elif duration.endswith("h"):
            return timedelta(hours=int(duration[:-1]))
        else:
            return timedelta(hours=1)

    def _format_summary(self, entries: list[LogEntry], component: str | None, last: str) -> str:
        """格式化摘要"""
        total = len(entries)
        errors = sum(1 for e in entries if e.level == LogLevel.ERROR)
        warnings = sum(1 for e in entries if e.level == LogLevel.WARNING)

        lines = [
            f"最近 {last} 的日志摘要",
            f"组件: {component or '全部'}",
            f"总计: {total} 条",
            f"错误: {errors} 条",
            f"警告: {warnings} 条",
        ]

        if errors > 0:
            lines.append("\n最近错误:")
            for e in [e for e in entries if e.level == LogLevel.ERROR][:5]:
                lines.append(f"  - [{e.component.value}] {e.msg}")

        return "\n".join(lines)

    def _format_entries(self, entries: list[LogEntry]) -> str:
        """格式化原始日志"""
        lines = []
        for e in entries[:20]:
            lines.append(f"[{e.ts.strftime('%H:%M:%S')}] {e.level.value:7} {e.component.value:10} {e.msg}")
        return "\n".join(lines)
```

### 4.6 CLI logs 命令

```python
# cli/commands.py

@app.command()
def logs(
    component: str = typer.Option(None, "--component", "-c", help="组件名称"),
    level: str = typer.Option(None, "--level", "-l", help="日志级别"),
    tail: bool = typer.Option(False, "--tail", "-t", help="实时 tail 模式"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续跟随"),
):
    """查看系统日志"""
    from nano_alice.log import ensure_logging_initialized, get_log_store, Component, LogLevel

    ensure_logging_initialized()  # 先初始化日志系统
    store = get_log_store()
    if not store:
        console.print("[red]日志系统初始化失败[/red]")
        return

    if tail or follow:
        _tail_logs(store, component, level, follow)
    else:
        _query_logs(store, component, level)

def _query_logs(store: LogStore, component: str | None, level: str | None):
    """查询模式：rich 表格输出"""
    entries = store.query(
        component=Component(component) if component else None,
        level=LogLevel(level) if level else None,
        since=datetime.now() - timedelta(hours=1),
        limit=50,
    )

    table = Table(title=f"日志 ({component or '全部'})")
    table.add_column("时间", style="dim")
    table.add_column("级别")
    table.add_column("组件")
    table.add_column("消息")

    for e in entries:
        level_color = {
            "ERROR": "red",
            "WARNING": "yellow",
            "INFO": "green",
        }.get(e.level.value, "")

        table.add_row(
            e.ts.strftime("%H:%M:%S"),
            f"[{level_color}]{e.level.value}[/{level_color}]",
            e.component.value,
            e.msg[:50],
        )

    console.print(table)
```

### 4.7 InternalState 扩展

```python
# agent/reflect/internal_state.py

@dataclass
class InternalState:
    # ... 现有字段 ...

    # 新增：日志指标
    error_count_last_hour: int = 0
    last_error_summary: str | None = None
    components_health: dict[str, str] = field(default_factory=dict)
    # {"telegram": "healthy", "scheduler": "healthy", "mcp.github": "degraded"}

    def reset_error_count(self) -> None:
        """重置错误计数（每小时调用）"""
        self.error_count_last_hour = 0
```

```python
# agent/signals/types.py

class AgentSignal(Enum):
    # ... 现有信号 ...
    LOG_ERROR = "log_error"  # 日志错误信号
```

```python
# agent/reflect/processor.py

class ReflectProcessor:
    async def _handle_log_error(self, signal: Signal) -> None:
        """处理日志错误信号，更新 InternalState"""
        self.state.error_count_last_hour += 1
        component = signal.data["component"]
        msg = signal.data["msg"]

        self.state.last_error_summary = f"{component}: {msg}"
        self.state.components_health[component] = "degraded"

        # 如果错误过多，触发自省
        if self.state.error_count_last_hour > 10:
            await self._trigger_self_reflect()
```

---

## 5. 实施计划

### Phase 1: 核心基础设施（无侵入）

**目标**：建立 LogStore，不影响现有代码。

| 文件 | 操作 | 说明 |
|------|------|------|
| `nano_alice/log/__init__.py` | 新增 | 单例初始化，组件推断 |
| `nano_alice/log/types.py` | 新增 | LogEntry, LogLevel, Component |
| `nano_alice/log/store.py` | 新增 | LogStore, _FileSink |
| `nano_alice/utils/helpers.py` | 修改 | 新增 get_logs_path() |

**验证**：
```python
# 手动测试
from nano_alice.log import ensure_logging_initialized
store = ensure_logging_initialized()
# 验证日志目录创建，文件写入
```

### Phase 2: 集成 entry points（小改动）

**目标**：所有入口调用初始化。

修改 `cli/commands.py`：
- `gateway()` 开头加 `ensure_logging_initialized()`
- `agent()` 开头加 `ensure_logging_initialized()`
- `cron_run()` 开头加 `ensure_logging_initialized()`
- `logs()` 开头加 `ensure_logging_initialized()` -- 独立进程查询也需要初始化

**验证**：
```bash
nano-alice agent -m "hello"  # 检查日志文件生成
nano-alice cron list         # 检查日志文件生成
nano-alice logs              # 独立进程查询，应正常工作
```

### Phase 3: LogsTool（Agent 可用）

**目标**：Agent 可以查询日志。

新增 `nano_alice/agent/tools/logs.py`，注册到 ToolRegistry。

**验证**：
```bash
nano-alice agent -m "用 logs 工具查看最近 30 分钟的日志"
```

### Phase 4: CLI logs 命令（人类可用）

**目标**：开发者可以查看日志。

修改 `cli/commands.py`，新增 `logs()` 子命令。

**验证**：
```bash
nano-alice logs --component scheduler
nano-alice logs --tail
```

### Phase 5: InternalState 集成（状态感知）

**目标**：Agent 可以自省系统健康。

修改：
- `agent/signals/types.py` - 新增 `LOG_ERROR` 信号
- `agent/reflect/processor.py` - 新增 `_handle_log_error()`
- `agent/reflect/internal_state.py` - 扩展字段

**验证**：
```bash
# 触发错误后检查 InternalState
nano-alice agent -m "查看当前系统状态"
```

---

## 6. 测试计划

### 6.1 单元测试

```python
# tests/log/test_types.py
def test_log_entry_serialization():
    entry = LogEntry(...)
    jsonl = entry.to_jsonl()
    restored = LogEntry.from_jsonl(jsonl)
    assert restored == entry

# tests/log/test_store.py
def test_write_and_query():
    store = LogStore(tmp_dir, retention_hours=6)
    entry = LogEntry(...)
    store.write(entry)

    results = store.query(component=Component.AGENT)
    assert len(results) == 1

def test_cleanup_removes_old_entries():
    store = LogStore(tmp_dir, retention_hours=6)
    # 写入旧日志
    old_entry = LogEntry(..., ts=datetime.now() - timedelta(hours=7))
    store.write(old_entry)
    # 清理
    store._cleanup(Component.AGENT)
    # 验证
    results = store.query(component=Component.AGENT)
    assert len(results) == 0
```

### 6.2 集成测试

```bash
# 启动 gateway
nano-alice gateway &

# 等待调度任务执行
sleep 60

# 检查日志
nano-alice logs --component scheduler

# 通过 agent 查询
nano-alice agent -m "用 logs 工具查看 scheduler 日志"
```

### 6.3 回归测试

```bash
# 确保现有功能不受影响
nano-alice agent -m "hello"
nano-alice cron list
nano-alice channels status
```

---

## 7. 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| 日志写入失败影响主流程 | try-catch + 警告，不抛异常 |
| 日志文件过大 | 6 小时轮转 + 写入时清理 |
| 性能影响 | 惰性清理 + 临时文件原子替换 |
| 兼容性问题 | 保留 loguru 默认 stderr 输出 |
| **logger.disable() 影响 file sink** | **移除 disable()，改用 console sink level** |
| **SignalBus.publish() 未 await** | **使用 asyncio.create_task() fire-and-forget** |
| **清理时并发写入冲突** | **每组件独立锁 + 临时文件 rename** |
| **query() limit 语义错误** | **先收集后排序再 limit** |
| **logs 命令未初始化** | **独立进程也调用 ensure_logging_initialized()** |

---

## 8. 未来扩展

- [ ] 日志聚合：支持分布式部署
- [ ] 告警规则：基于日志模式触发告警
- [ ] 日志可视化：Web UI
- [ ] 导出功能：导出为 CSV/JSON
- [ ] 搜索索引：全文检索

---

## 9. 变更历史

| 日期 | 版本 | 变更 |
|------|------|------|
| 2025-03-23 | 0.1 | 初版设计 |
| 2025-03-23 | 0.2 | 修复严重问题：logs CLI 初始化、logger.disable() 并发安全、SignalBus 调用、query() 语义 |
| 2025-03-23 | 0.3 | 澄清 logger.remove() 与 logger.disable() 的关系：用 logger.remove() 重置后分别配置 sink，console 控级别，file 始终写 |
