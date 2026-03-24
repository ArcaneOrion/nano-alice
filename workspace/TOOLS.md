# Available Tools

This document describes the tools available to nano-alice.

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
web_fetch(url: str, extract_mode: str = "markdown", max_chars: int = 50000) -> str
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

## Scheduled Reminders (Scheduler)

Use the `scheduler` tool to schedule reminders:

### Set a recurring reminder
```python
scheduler(action="add", message="Good morning! ☀️", cron_expr="0 9 * * *", tz="Asia/Shanghai")
scheduler(action="add", message="Drink water! 💧", every_seconds=7200)  # every 2 hours
```

### Set a one-time reminder
```python
scheduler(action="add", message="Meeting starts now!", at="2026-03-24T15:00:00")
```

### Manage reminders
```python
scheduler(action="list")              # List all jobs
scheduler(action="remove", job_id="<job_id>")   # Remove a job
```

**Note**: The scheduler tool automatically uses your current session context (channel/chat_id) for delivery.

## Periodic Task Management

The `TODO.md` file in the workspace is checked every 30 minutes.
Use file operations to manage periodic tasks:

### Add a periodic task
```python
# Append a new task
edit_file(
    path="TODO.md",
    old_text="## Example Tasks",
    new_text="- [ ] New periodic task here\n\n## Example Tasks"
)
```

### Remove a periodic task
```python
# Remove a specific task
edit_file(
    path="TODO.md",
    old_text="- [ ] Task to remove\n",
    new_text=""
)
```

### Rewrite all tasks
```python
# Replace the entire file
write_file(
    path="TODO.md",
    content="# Periodic Tasks\n\n- [ ] Task 1\n- [ ] Task 2\n"
)
```

---

## Adding Custom Tools

To add custom tools:
1. Create a class that extends `Tool` in `nano_alice/agent/tools/`
2. Implement `name`, `description`, `parameters`, and `execute`
3. Register it in `AgentLoop._register_default_tools()`
