# Available Tools

This document describes the tools available to nanobot.

## File Operations

### read_file
Read the contents of a file.
```
read_file(path: str) -> str
```

### write_file
Write content to a file (creates parent directories if needed).
```
write_file(path: str, content: str) -> str
```

### edit_file
Edit a file by replacing specific text.
```
edit_file(path: str, old_text: str, new_text: str) -> str
```

### list_dir
List contents of a directory.
```
list_dir(path: str) -> str
```

## Shell Execution

### exec
Execute a shell command and return output.
```
exec(command: str, working_dir: str = None) -> str
```

**Safety Notes:**
- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- Optional `restrictToWorkspace` config to limit paths

## Web Access

### web_search
Search the web using Brave Search API.
```
web_search(query: str, count: int = 5) -> str
```

Returns search results with titles, URLs, and snippets. Requires `tools.web.search.apiKey` in config.

### web_fetch
Fetch and extract main content from a URL.
```
web_fetch(url: str, extractMode: str = "markdown", maxChars: int = 50000) -> str
```

**Notes:**
- Content is extracted using readability
- Supports markdown or plain text extraction
- Output is truncated at 50,000 characters by default

## Communication

### message
Send a message to the user (used internally).
```
message(content: str, channel: str = None, chat_id: str = None) -> str
```

## Background Tasks

### spawn
Spawn a subagent to handle a task in the background.
```
spawn(task: str, label: str = None) -> str
```

Use for complex or time-consuming tasks that can run independently. The subagent will complete the task and report back when done.

## Scheduled Reminders (Cron)

### cron tool

Use the `cron` tool to schedule reminders and recurring tasks.

定时任务到期时，消息以 `[定时任务: {name}] {message}` 格式出现在你的对话中。像处理用户消息一样处理它——理解内容、执行操作、回复用户。回复自动发送到创建任务时的频道。

```
cron(action="add", message="Good morning! ☀️", cron_expr="0 9 * * *")
cron(action="add", message="Drink water! 💧", every_seconds=7200)
cron(action="add", message="Meeting starts now!", at="2025-01-31T15:00:00")
cron(action="list")
cron(action="remove", job_id="abc123")
```

## Heartbeat Task Management

The `HEARTBEAT.md` file in the workspace is checked every 30 minutes.
Use file operations to manage periodic tasks:

### Add a heartbeat task
```python
# Append a new task
edit_file(
    path="HEARTBEAT.md",
    old_text="## Example Tasks",
    new_text="- [ ] New periodic task here\n\n## Example Tasks"
)
```

### Remove a heartbeat task
```python
# Remove a specific task
edit_file(
    path="HEARTBEAT.md",
    old_text="- [ ] Task to remove\n",
    new_text=""
)
```

### Rewrite all tasks
```python
# Replace the entire file
write_file(
    path="HEARTBEAT.md",
    content="# Heartbeat Tasks\n\n- [ ] Task 1\n- [ ] Task 2\n"
)
```

---

## 截图（Screenshot）

本系统运行在 **Niri（Wayland 合成器）** 上。X11 截图工具（scrot、import、gnome-screenshot）在 Wayland 下**无法工作**，会产生黑屏。

### 截图命令

优先使用系统已安装的 `grim`：
```bash
grim /tmp/screenshot.png
```

如果 `grim` 不在 PATH 中，通过 nix-shell 调用：
```bash
nix-shell -p grim --run "grim /tmp/screenshot.png"
```

### 区域截图（需要 slurp）
```bash
grim -g "$(slurp)" /tmp/screenshot.png
# 或
nix-shell -p grim -p slurp --run 'grim -g "$(slurp)" /tmp/screenshot.png'
```

### 重要提示
- **禁止使用** scrot、import、gnome-screenshot 等 X11 工具
- 截图后可通过 `message` 工具的 `media` 参数发送图片

---

## Adding Custom Tools

To add custom tools:
1. Create a class that extends `Tool` in `nanobot/agent/tools/`
2. Implement `name`, `description`, `parameters`, and `execute`
3. Register it in `AgentLoop._register_default_tools()`
