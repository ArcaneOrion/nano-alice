"""CLI commands for nano-alice."""

import asyncio
import os
import signal
from pathlib import Path
import select
import sys

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from nano_alice import __version__, __logo__
from nano_alice.config.schema import Config

app = typer.Typer(
    name="nano-alice",
    help=f"{__logo__} nano-alice - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    history_file = Path.home() / ".nano-alice" / "history" / "cli_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} nano-alice[/cyan]")
    console.print(body)
    console.print()


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} nano-alice v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """nano-alice - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize nano-alice configuration and workspace."""
    from nano_alice.config.loader import get_config_path, load_config, save_config
    from nano_alice.config.schema import Config
    from nano_alice.utils.helpers import get_workspace_path
    
    config_path = get_config_path()
    
    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
        console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
        if typer.confirm("Overwrite?"):
            config = Config()
            save_config(config)
            console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
        else:
            config = load_config()
            save_config(config)
            console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        save_config(Config())
        console.print(f"[green]✓[/green] Created config at {config_path}")
    
    # Create workspace
    workspace = get_workspace_path()
    
    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace}")
    
    # Create default bootstrap files
    _create_workspace_templates(workspace)
    
    console.print(f"\n{__logo__} nano-alice is ready!")
    console.print("\nNext steps:")
    console.print("  1. Configure your model provider in [cyan]~/.nano-alice/config.json[/cyan]")
    console.print("     Or override sensitive fields with [cyan]NANO_ALICE_*[/cyan] environment variables")
    console.print("     Default model is [cyan]anthropic/claude-opus-4-5[/cyan], so add the matching provider key")
    console.print("  2. Chat: [cyan]nano-alice agent -m \"Hello!\"[/cyan]")
    console.print("  3. If you enable a chat channel, set [cyan]allowFrom[/cyan] to tighten who can reach the bot")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/nano-alice#-chat-apps[/dim]")




def _workspace_bootstrap_templates() -> dict[str, str]:
    """Return default top-level workspace bootstrap templates."""
    return {
        "AGENTS.md": """<agents>
  <!-- 文件分工：定义 agent 的全局协作原则、行为规范，以及如何理解 memory / skills 的入口关系。 -->
  <description>你是一个有用的 AI 助手。回答简洁、准确、友好。</description>

  <behavior>
    <rule>执行操作前先说明要做什么</rule>
    <rule>请求不明确时主动询问</rule>
    <rule>善用工具完成任务</rule>
    <rule>重要信息记录到 memory/MEMORY.md；过往事件记录到 memory/HISTORY.md</rule>
    <rule>当用户需要周期性提醒、后台巡检或定时续跑任务时，主动维护 workspace 根目录下的 `HEARTBEAT.md`</rule>
  </behavior>

  <memory>
    <description>MEMORY.md 每轮自动加载，包含核心事实和文件索引。详细内容按需从子文件中读取。</description>
  </memory>

  <skills>
    <description>你可以使用 workspace 中的技能扩展能力，优先读取 skills/{skill-name}/SKILL.md。</description>
  </skills>
</agents>
""",
        "SOUL.md": """<soul>
  <!-- 文件分工：定义 agent 的气质、价值观、沟通风格，不承担具体工具规则或用户事实记录。 -->
  <identity>我是 nano-alice，一个轻量级但可靠的 AI 助手。</identity>

  <personality>
    <trait>乐于助人，态度友好</trait>
    <trait>简洁直接，不空转</trait>
    <trait>愿意持续学习和迭代</trait>
  </personality>

  <values>
    <value>准确优先于速度</value>
    <value>尊重用户隐私与安全</value>
    <value>行动透明，不假装已经完成</value>
  </values>
</soul>
""",
        "USER.md": """<user>
  <!-- 文件分工：记录用户画像和稳定偏好；项目状态、临时上下文、经验教训不要堆在这里。 -->
  <description>这里记录用户的长期事实与稳定偏好。</description>

  <preferences>
    <pref key="沟通风格">待补充</pref>
    <pref key="时区">待补充</pref>
    <pref key="语言">待补充</pref>
  </preferences>
</user>
""",
        "IDENTITY.md": """<identity>
  <!-- 文件分工：定义 agent 对“我是谁、职责边界是什么、哪些原则不能破”的稳定自我认知。 -->
  <role>
    <name>nano-alice</name>
    <summary>我是一个可靠的个人 AI 助手，负责帮助用户完成任务、维护连续上下文，并诚实反馈能力边界。</summary>
  </role>

  <responsibilities>
    <item>优先提供清晰、诚实、可执行的帮助</item>
    <item>在多轮任务中保持状态连续，不轻易丢失上下文</item>
    <item>将内部调度、提醒、任务续跑与对外对话区分开</item>
  </responsibilities>

  <guardrails>
    <rule>数据要有引用来源，保证源头可追溯</rule>
    <rule>默认简洁表达，但复杂任务要保留必要细节</rule>
  </guardrails>
</identity>
""",
        "TOOLS.md": """<tools>
  <!-- 文件分工：补充说明工具的非显然约束、推荐用法和当前语义；不是完整 API 文档。 -->
  <description>Tool signatures are provided automatically via function calling. This file documents non-obvious constraints, current semantics, and usage patterns.</description>

  <tool name="exec">
    <safety>
      <limit>Commands have a configurable timeout (default 60s)</limit>
      <limit>Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)</limit>
      <limit>Output is truncated at 10,000 characters</limit>
      <limit>`restrictToWorkspace` config can limit file access to the workspace</limit>
    </safety>
  </tool>

  <tool name="cron">
    <description>Use the cron tool or `nano-alice cron ...` CLI to manage the agent's internal scheduled jobs. Cron is primarily an internal scheduler / self-wakeup mechanism: when a job becomes due, it should be treated as an internal reminder intent/event flow rather than an ordinary inbound user message. It can be used to implement reminders, recurring tasks, and delayed follow-ups.</description>
    <examples>
      <example command="nano-alice cron add --name 'morning' --message 'Good morning!' --cron '0 9 * * *'">Recurring: every day at 9am</example>
      <example command="nano-alice cron add --name 'standup' --message 'Standup time!' --cron '0 10 * * 1-5' --tz 'Asia/Shanghai'">With timezone</example>
      <example command="nano-alice cron add --name 'water' --message 'Drink water!' --every 7200">Recurring: every 2 hours</example>
      <example command="nano-alice cron add --name 'meeting' --message 'Meeting starts now!' --at '2030-01-01T15:00:00'">One-time: specific ISO time in the future (replace with your actual target time)</example>
      <example command="nano-alice cron list">List jobs</example>
      <example command="nano-alice cron run &lt;job_id&gt;">Run a job immediately for verification</example>
      <example command="nano-alice cron remove &lt;job_id&gt;">Remove a job</example>
    </examples>
    <notes>
      <note>For periodic background checks that the agent should re-read on each heartbeat tick, keep the standing instructions in `HEARTBEAT.md` at the workspace root.</note>
      <note>`HEARTBEAT.md` is the heartbeat workflow entrypoint: store durable polling instructions, update them when the task changes, and remove or clear them when the task is finished.</note>
    </notes>
  </tool>

  <tool name="message">
    <description>Sending a message is not the same as confirmed delivery. Prefer returning or recording message identifiers / receipts when the channel supports them.</description>
  </tool>
</tools>
""",
        "HEARTBEAT.md": """# Heartbeat Tasks

- Use this file for durable background checks, recurring follow-ups, and tasks the agent should revisit on heartbeat ticks.
- Keep only active instructions here. Remove or clear completed tasks so heartbeat can stay quiet.
- Prefer concrete checks, cadence expectations, and push criteria.

## Examples

- Every workday morning, check today's calendar and draft a concise agenda if there are important events.
- Watch a long-running job and notify the user only when it finishes or fails.
- Re-check a waiting dependency every 30 minutes and push an update only when status changes.
""",
    }


def _workspace_memory_templates() -> dict[str, str]:
    """Return default workspace memory templates."""
    return {
        "MEMORY.md": """# Long-term Memory

## Purpose

- Store long-term facts, stable preferences, and system-level rules.
- Keep this file concise because it is loaded every turn.
- Put project status in `projects.md`, milestones in `HISTORY.md`, and short-lived context in `SCRATCH.md`.

## User Facts

- (Add stable facts here)

## Preferences

- (Add stable preferences here)

## System Rules

- (Add durable behavioral rules here)
""",
        "HISTORY.md": """# History

- Record important events, milestones, reversals, and confirmations here.
""",
        "SCRATCH.md": """# Scratch

- Use this file for short-term context, current focus, and next checks.
- It is expected to be rewritten frequently.
""",
        "projects.md": """# Projects

- Track active projects, current status, and next steps here.
- Do not put one-off failures or temporary debugging notes here.
""",
        "lessons.md": """# Lessons

- Capture reusable lessons and stable operational rules here.
""",
        "schedule.md": """# Schedule

- Put class schedules, recurring reminders, and durable timing rules here.
""",
    }


def _create_workspace_templates(workspace: Path):
    """Create default workspace template files."""
    for filename, content in _workspace_bootstrap_templates().items():
        file_path = workspace / filename
        if not file_path.exists():
            file_path.write_text(content, encoding="utf-8")
            console.print(f"  [dim]Created {filename}[/dim]")
    
    # Create memory directory and MEMORY.md
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    for filename, content in _workspace_memory_templates().items():
        file_path = memory_dir / filename
        if not file_path.exists():
            file_path.write_text(content, encoding="utf-8")
            console.print(f"  [dim]Created memory/{filename}[/dim]")

    # Create skills directory for custom user skills
    skills_dir = workspace / "skills"
    skills_dir.mkdir(exist_ok=True)


def _make_provider(
    config: Config,
    model: str | None = None,
    provider_name: str | None = None,
):
    """Create the appropriate LLM provider from config."""
    from nano_alice.providers.litellm_provider import LiteLLMProvider
    from nano_alice.providers.openai_codex_provider import OpenAICodexProvider
    from nano_alice.providers.custom_provider import CustomProvider
    from nano_alice.providers.rotating_provider import RotatingProvider

    def _build_single_provider(resolved_model: str, resolved_provider_name: str | None = None):
        resolved_provider_name = config.get_provider_name(resolved_model, resolved_provider_name)
        provider_config = config.get_provider(resolved_model, resolved_provider_name)

        if resolved_provider_name == "openai_codex":
            if provider_config and (provider_config.api_key or provider_config.api_base):
                return OpenAICodexProvider(
                    default_model=resolved_model,
                    api_key=provider_config.api_key if provider_config else None,
                    api_base=config.get_api_base(resolved_model, resolved_provider_name),
                    extra_headers=provider_config.extra_headers if provider_config else None,
                )
            return OpenAICodexProvider(default_model=resolved_model)

        if resolved_provider_name in Config.OPENAI_ROUTE_NAMES:
            return CustomProvider(
                api_key=provider_config.api_key if provider_config else "no-key",
                api_base=config.get_api_base(resolved_model, resolved_provider_name) or "https://api.openai.com/v1",
                default_model=resolved_model.split("/", 1)[1] if "/" in resolved_model else resolved_model,
            )

        if resolved_provider_name == "openai" and provider_config and provider_config.api_base:
            custom_model = resolved_model.split("/", 1)[1] if resolved_model.lower().startswith("openai/") else resolved_model
            return CustomProvider(
                api_key=provider_config.api_key if provider_config else "no-key",
                api_base=config.get_api_base(resolved_model, resolved_provider_name) or "https://api.openai.com/v1",
                default_model=custom_model,
            )

        if resolved_provider_name == "custom":
            return CustomProvider(
                api_key=provider_config.api_key if provider_config else "no-key",
                api_base=config.get_api_base(resolved_model, resolved_provider_name) or "http://localhost:8000/v1",
                default_model=resolved_model,
            )

        from nano_alice.providers.registry import find_by_name
        spec = find_by_name(resolved_provider_name)
        if not resolved_model.startswith("bedrock/") and not (provider_config and provider_config.api_key) and not (spec and spec.is_oauth):
            console.print("[red]Error: No API key configured.[/red]")
            console.print("Set one in ~/.nano-alice/config.json under providers section")
            raise typer.Exit(1)

        return LiteLLMProvider(
            api_key=provider_config.api_key if provider_config else None,
            api_base=config.get_api_base(resolved_model, resolved_provider_name),
            default_model=resolved_model,
            extra_headers=provider_config.extra_headers if provider_config else None,
            provider_name=resolved_provider_name,
        )

    def _normalize_model_identifier(value: str) -> str:
        if "/" not in value:
            return value.strip().lower()
        prefix, remainder = value.split("/", 1)
        normalized_prefix = config._normalize_provider_key(prefix) or prefix.lower().replace("-", "_")
        return f"{normalized_prefix}/{remainder}"

    def _build_provider_pool(
        primary_model: str,
        candidate_models: list[str],
        fallback_timeout_seconds: float,
    ):
        normalized_primary = _normalize_model_identifier(primary_model)
        fallback_models: list[str] = []
        seen_fallbacks: set[str] = set()
        for candidate in candidate_models:
            normalized_candidate = _normalize_model_identifier(candidate)
            if normalized_candidate == normalized_primary or normalized_candidate in seen_fallbacks:
                continue
            seen_fallbacks.add(normalized_candidate)
            fallback_models.append(candidate)

        primary_provider = _build_single_provider(primary_model)
        fallback_providers = [_build_single_provider(candidate) for candidate in fallback_models]
        return RotatingProvider(
            primary_provider,
            fallback_providers,
            fallback_timeout_seconds=fallback_timeout_seconds,
        )

    using_default_model = model is None
    default_models = config.agents.defaults.models if using_default_model else []
    if using_default_model and default_models:
        primary_model = _resolve_default_agent_model(config)

        return _build_provider_pool(
            primary_model,
            default_models,
            float(config.agents.defaults.fallback_timeout_seconds),
        )

    model = model or config.agents.defaults.model
    return _build_single_provider(model, provider_name)


def _make_memory_provider(config: Config):
    """Create a separate provider for the memory subagent, or None to reuse main."""
    from nano_alice.providers.custom_provider import CustomProvider

    memory_cfg = config.agents.memory
    if not memory_cfg.enabled or not memory_cfg.model:
        return None

    # Explicit api_key + api_base → CustomProvider (OpenAI-compatible)
    if memory_cfg.api_key and memory_cfg.api_base:
        return CustomProvider(
            api_key=memory_cfg.api_key,
            api_base=memory_cfg.api_base,
            default_model=memory_cfg.model,
        )

    # Model set but no explicit credentials → auto-match from providers
    return _make_provider(config, memory_cfg.model, None)


def _resolve_default_agent_model(config: Config) -> str:
    """Resolve the primary model used by the main agent loop."""
    default_models = config.agents.defaults.models
    configured_model = config.agents.defaults.model
    model_fields_set = getattr(config.agents.defaults, "model_fields_set", set())
    if default_models and "model" not in model_fields_set:
        return default_models[0]
    return configured_model


def _make_subagent_provider(config: Config, inherited_model: str | None = None):
    """Create a separate provider for task subagents, or None to reuse main."""
    from nano_alice.providers.rotating_provider import RotatingProvider

    subagent_cfg = config.agents.subagent
    configured_models = [candidate for candidate in subagent_cfg.models if candidate]
    configured_model = subagent_cfg.model.strip() if subagent_cfg.model else ""
    has_explicit_subagent_config = bool(configured_model or configured_models)

    if not has_explicit_subagent_config:
        return None

    primary_model = configured_model or (configured_models[0] if configured_models else "") or (inherited_model or "")
    if not primary_model:
        return None

    fallback_models = configured_models if configured_models else [primary_model]

    if configured_models:
        primary_provider = _make_provider(config, primary_model, None)
        fallback_providers = [
            _make_provider(config, candidate, None)
            for candidate in configured_models
            if candidate != primary_model
        ]
        return RotatingProvider(
            primary_provider,
            fallback_providers,
            fallback_timeout_seconds=float(subagent_cfg.fallback_timeout_seconds),
        )

    return _make_provider(config, primary_model, None)


def _setup_logging(enable_console: bool = False, console_level: str = "INFO") -> None:
    """配置 loguru 日志：文件持久化 + 可选控制台输出。

    Args:
        enable_console: 是否开启控制台输出
        console_level: 控制台日志级别，默认 INFO，gateway 模式下为 DEBUG
    """
    from loguru import logger
    from nano_alice.config.loader import get_data_dir

    # Clear loguru's default stderr sink and any prior nano-alice sinks so
    # repeated CLI/gateway setup does not duplicate every log line.
    logger.remove()

    # 先确保模块启用（之前可能被 disable 过）
    logger.enable("nano_alice")

    log_dir = get_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 文件日志：始终开启，按大小轮转，保留最近 7 天
    logger.add(
        log_dir / "nano-alice.log",
        rotation="10 MB",
        retention="7 days",
        compression="gz",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}",
        filter="nano_alice",
    )

    if enable_console:
        # 控制台：显示指定级别（gateway 用 DEBUG，cli 用 INFO）
        logger.add(
            lambda msg: print(msg, end=""),
            level=console_level,
            format="<level>{time:HH:mm:ss} | {level:<5} | {message}</level>",
            filter="nano_alice",
            colorize=True,
        )


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the nano-alice gateway."""
    from nano_alice.config.loader import load_config, get_data_dir
    from nano_alice.bus.queue import MessageBus
    from nano_alice.agent.loop import AgentLoop
    from nano_alice.agent.reminder_intent import ReminderIntentStore
    from nano_alice.channels.manager import ChannelManager
    from nano_alice.session.manager import SessionManager
    from nano_alice.cron.service import CronService
    from nano_alice.cron.types import CronJob
    from nano_alice.heartbeat.service import HeartbeatService
    
    _setup_logging(enable_console=True, console_level="DEBUG" if verbose else "INFO")

    # 第三方库（litellm 等）使用 stdlib logging，verbose 时也开启
    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting nano-alice gateway on port {port}...")
    
    config = load_config()
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # Memory subagent provider (separate credentials when configured)
    memory_provider = _make_memory_provider(config)
    default_model = _resolve_default_agent_model(config)
    subagent_provider = _make_subagent_provider(config, inherited_model=default_model)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path, intent_store=ReminderIntentStore(config.workspace_path))

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=default_model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        brave_api_key=config.tools.web.search.api_key or None,
        tavily_api_key=config.tools.web.search.tavily_api_key or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        embeddings_config=config.tools.embeddings,
        memory_agent_config=config.agents.memory,
        memory_provider=memory_provider,
        subagent_provider=subagent_provider,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> None:
        """将 cron 任务作为内部提醒事件注入 MessageBus。"""
        from nano_alice.bus.events import InboundMessage
        channel = job.payload.channel or "cli"
        chat_id = job.payload.to or "direct"
        await bus.publish_inbound(InboundMessage(
            channel="system",
            sender_id="cron",
            chat_id=f"{channel}:{chat_id}",
            content="",
            metadata={
                "_cron_intent_due": True,
                "_cron_job_id": job.id,
                "_intent_id": job.payload.intent_id,
                "_session_key": f"{channel}:{chat_id}",
                "_origin_channel": channel,
                "_origin_chat_id": chat_id,
            },
        ))
    cron.on_job = on_cron_job
    
    # Create channel manager (before heartbeat so auto-detect can use enabled channels)
    channels = ChannelManager(config, bus)

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    # Create heartbeat service
    async def on_heartbeat(prompt: str, channel: str, chat_id: str) -> str:
        """Publish heartbeat as inbound message so run() dispatches the response."""
        from nano_alice.bus.events import InboundMessage
        if channel and chat_id:
            await bus.publish_inbound(InboundMessage(
                channel=channel,
                sender_id="heartbeat",
                chat_id=chat_id,
                content=prompt,
                metadata={"_session_key": "heartbeat"},
            ))
            return ""
        # No target channel — fall back to process_direct (response not dispatched)
        return await agent.process_direct(prompt, session_key="heartbeat")

    hb_cfg = config.heartbeat
    # Auto-detect notify target from first enabled channel if not configured
    notify_ch, notify_id = hb_cfg.notify_channel, hb_cfg.notify_chat_id
    if not notify_ch:
        for ch_name in channels.enabled_channels:
            ch_config = getattr(config.channels, ch_name, None)
            if ch_config and getattr(ch_config, "allow_from", None):
                notify_ch = ch_name
                notify_id = ch_config.allow_from[0]
                break

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        notify_channel=notify_ch,
        notify_chat_id=notify_id,
    )

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    hb_target = f" → {notify_ch}:{notify_id}" if notify_ch else " (no target channel)"
    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s // 60}m{hb_target}")
    
    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()
    
    asyncio.run(run())




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show nano-alice runtime logs during chat"),
):
    """Interact with the agent directly."""
    from nano_alice.config.loader import load_config, get_data_dir
    from nano_alice.bus.queue import MessageBus
    from nano_alice.agent.loop import AgentLoop
    from nano_alice.cron.service import CronService
    
    config = load_config()

    bus = MessageBus()
    provider = _make_provider(config)

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Memory subagent provider
    memory_provider = _make_memory_provider(config)
    default_model = _resolve_default_agent_model(config)
    subagent_provider = _make_subagent_provider(config, inherited_model=default_model)

    _setup_logging(enable_console=logs, console_level="INFO")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=default_model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        brave_api_key=config.tools.web.search.api_key or None,
        tavily_api_key=config.tools.web.search.tavily_api_key or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        embeddings_config=config.tools.embeddings,
        memory_agent_config=config.agents.memory,
        memory_provider=memory_provider,
        subagent_provider=subagent_provider,
    )

    # Show spinner when logs are off (no output to miss); skip when logs are on
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext
            return nullcontext()
        # Animated spinner is safe to use with prompt_toolkit input handling
        return console.status("[dim]nano-alice is thinking...[/dim]", spinner="dots")

    async def _cli_progress(content: str) -> None:
        console.print(f"  [dim]↳ {content}[/dim]")

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            with _thinking_ctx():
                response = await agent_loop.process_direct(message, session_id, on_progress=_cli_progress)
            _print_agent_response(response, render_markdown=markdown)
            await agent_loop.await_pending()
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from nano_alice.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _exit_on_sigint(signum, frame):
            _restore_terminal()
            console.print("\nGoodbye!")
            os._exit(0)

        signal.signal(signal.SIGINT, _exit_on_sigint)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            console.print(f"  [dim]↳ {msg.content}[/dim]")
                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            console.print()
                            _print_agent_response(msg.content, render_markdown=markdown)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                        ))

                        with _thinking_ctx():
                            await turn_done.wait()

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.await_pending()
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from nano_alice.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # WhatsApp
    wa = config.channels.whatsapp
    table.add_row(
        "WhatsApp",
        "✓" if wa.enabled else "✗",
        wa.bridge_url
    )

    dc = config.channels.discord
    table.add_row(
        "Discord",
        "✓" if dc.enabled else "✗",
        dc.gateway_url
    )

    # Feishu
    fs = config.channels.feishu
    fs_config = f"app_id: {fs.app_id[:10]}..." if fs.app_id else "[dim]not configured[/dim]"
    table.add_row(
        "Feishu",
        "✓" if fs.enabled else "✗",
        fs_config
    )

    # Mochat
    mc = config.channels.mochat
    mc_base = mc.base_url or "[dim]not configured[/dim]"
    table.add_row(
        "Mochat",
        "✓" if mc.enabled else "✗",
        mc_base
    )
    
    # Telegram
    tg = config.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row(
        "Telegram",
        "✓" if tg.enabled else "✗",
        tg_config
    )

    # Slack
    slack = config.channels.slack
    slack_config = "socket" if slack.app_token and slack.bot_token else "[dim]not configured[/dim]"
    table.add_row(
        "Slack",
        "✓" if slack.enabled else "✗",
        slack_config
    )

    # DingTalk
    dt = config.channels.dingtalk
    dt_config = f"client_id: {dt.client_id[:10]}..." if dt.client_id else "[dim]not configured[/dim]"
    table.add_row(
        "DingTalk",
        "✓" if dt.enabled else "✗",
        dt_config
    )

    # QQ
    qq = config.channels.qq
    qq_config = f"app_id: {qq.app_id[:10]}..." if qq.app_id else "[dim]not configured[/dim]"
    table.add_row(
        "QQ",
        "✓" if qq.enabled else "✗",
        qq_config
    )

    # Email
    em = config.channels.email
    em_config = em.imap_host if em.imap_host else "[dim]not configured[/dim]"
    table.add_row(
        "Email",
        "✓" if em.enabled else "✗",
        em_config
    )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess
    
    # User's bridge location
    user_bridge = Path.home() / ".nano-alice" / "bridge"
    
    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge
    
    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)
    
    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # nano_alice/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)
    
    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge
    
    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install -e .")
        raise typer.Exit(1)
    
    console.print(f"{__logo__} Setting up bridge...")
    
    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))
    
    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)
        
        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)
        
        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)
    
    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import subprocess
    from nano_alice.config.loader import load_config
    
    config = load_config()
    bridge_dir = _get_bridge_dir()
    
    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")
    
    env = {**os.environ}
    if config.channels.whatsapp.bridge_token:
        env["BRIDGE_TOKEN"] = config.channels.whatsapp.bridge_token
    
    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List scheduled jobs."""
    from nano_alice.config.loader import get_data_dir
    from nano_alice.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    jobs = service.list_jobs(include_disabled=all)
    
    if not jobs:
        console.print("No scheduled jobs.")
        return
    
    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")
    
    import time
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    for job in jobs:
        # Format schedule
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = f"{job.schedule.expr or ''} ({job.schedule.tz})" if job.schedule.tz else (job.schedule.expr or "")
        else:
            sched = "one-time"
        
        # Format next run
        next_run = ""
        if job.state.next_run_at_ms:
            ts = job.state.next_run_at_ms / 1000
            try:
                tz = ZoneInfo(job.schedule.tz) if job.schedule.tz else None
                next_run = _dt.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")
            except Exception:
                next_run = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        
        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"
        
        table.add_row(job.id, job.name, sched, status, next_run)
    
    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    tz: str | None = typer.Option(None, "--tz", help="IANA timezone for cron (e.g. 'America/Vancouver')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(None, "--channel", help="Channel for delivery (e.g. 'telegram', 'whatsapp')"),
):
    """Add a scheduled job."""
    from nano_alice.config.loader import get_data_dir
    from nano_alice.cron.service import CronService
    from nano_alice.cron.types import CronSchedule
    
    if tz and not cron_expr:
        console.print("[red]Error: --tz can only be used with --cron[/red]")
        raise typer.Exit(1)

    # Determine schedule type
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
    elif at:
        import datetime
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    try:
        job = service.add_job(
            name=name,
            schedule=schedule,
            message=message,
            to=to,
            channel=channel,
        )
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e

    console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
):
    """Remove a scheduled job."""
    from nano_alice.config.loader import get_data_dir
    from nano_alice.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
):
    """Enable or disable a job."""
    from nano_alice.config.loader import get_data_dir
    from nano_alice.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    force: bool = typer.Option(False, "--force", "-f", help="Run even if disabled"),
):
    """Manually run a job."""
    from loguru import logger
    from nano_alice.config.loader import load_config, get_data_dir
    from nano_alice.cron.service import CronService
    from nano_alice.cron.types import CronJob
    from nano_alice.bus.queue import MessageBus
    from nano_alice.agent.loop import AgentLoop
    _setup_logging(enable_console=False)

    config = load_config()
    provider = _make_provider(config)
    bus = MessageBus()

    memory_provider = _make_memory_provider(config)
    default_model = _resolve_default_agent_model(config)
    subagent_provider = _make_subagent_provider(config, inherited_model=default_model)

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=default_model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        brave_api_key=config.tools.web.search.api_key or None,
        tavily_api_key=config.tools.web.search.tavily_api_key or None,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        embeddings_config=config.tools.embeddings,
        memory_agent_config=config.agents.memory,
        memory_provider=memory_provider,
        subagent_provider=subagent_provider,
    )

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    result_holder = []

    async def on_job(job: CronJob) -> None:
        response = await agent_loop.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
        result_holder.append(response)

    service.on_job = on_job

    async def run():
        return await service.run_job(job_id, force=force)

    if asyncio.run(run()):
        console.print("[green]✓[/green] Job executed")
        if result_holder:
            _print_agent_response(result_holder[0], render_markdown=True)
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show nano-alice status."""
    from nano_alice.config.loader import load_config, get_config_path

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} nano-alice Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from nano_alice.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")
        
        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    from nano_alice.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
