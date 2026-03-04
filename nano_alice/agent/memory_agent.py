"""Background memory subagent that extracts info from recent conversations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from nano_alice.config.schema import EmbeddingsConfig
    from nano_alice.providers.base import LLMProvider

_SYSTEM_PROMPT = """\
You are a memory extraction agent. Analyze recent conversation and maintain memory files.

## Workspace
Memory directory: {memory_dir}

## Efficiency rules — IMPORTANT
- **Batch tool calls**: call multiple tools in one round when possible (e.g. read 2 files at once).
- **SCRATCH.md is append-only**: do NOT read it before appending. Use memory_search to check \
for duplicates instead.
- **Skip trivial conversations**: greetings, small talk, "1+1=?" → do NOT write anything, \
just reply with a summary and STOP.
- **Large files are tail-truncated**: read_file shows the header + the most recent content. \
If you need older content, use memory_search.
- **Finish quickly**: aim for ≤5 tool-call rounds. Stop as soon as writes are done.

## Instructions

### 1. Decide if anything is worth recording
Read the conversation. If it's only greetings/small talk/trivial Q&A, reply "Nothing notable" \
and STOP (zero tool calls).

### 2. Check for duplicates
Use memory_search (1-2 short queries, 5-10 words each) to see if the key facts already exist. \
If they do, skip writing. Do NOT search more than twice.

### 3. Write to the right file
**MEMORY.md（主文件，≤5KB，每轮全量注入 system prompt）**
- Core user facts, preferences, capabilities summary, file index table.
- Only add truly new long-term facts here.

**Sub-files（通过 RAG 按需召回）**
- `memory/schedule.md` — course schedule, class times
- `memory/projects.md` — active project status and TODOs
- `memory/lessons.md` — lessons learned, mistakes to avoid
- `memory/SCRATCH.md` — timestamped conversation summaries (append-only!)
- Other topic files as needed (create and add to MEMORY.md file index)

Rule: frequently needed → MEMORY.md. Detailed/topic-specific → sub-file.

For SCRATCH.md, use **append_file only**. Format:
```
### [YYYY-MM-DD HH:MM] Brief summary
- Key point 1
- Key point 2
```

### 4. Clean up stale info (only if the conversation contradicts existing records)
If a fact changed (status, preference, config), use edit_file to update it. \
Do NOT proactively scan all files for staleness — only fix what the current conversation \
explicitly contradicts. Keep MEMORY.md ≤5KB.

### 5. Stop
Reply with a one-line summary. Do NOT continue calling tools after writes are done."""

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file (overwrites).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace."},
                    "content": {"type": "string", "description": "File content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace old_string with new_string in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace."},
                    "old_string": {"type": "string", "description": "Text to find."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List directory contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to workspace."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append content to the end of a file. Creates the file if it doesn't exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace."},
                    "content": {"type": "string", "description": "Content to append."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Semantic search over memory files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language query."},
                    "top_k": {"type": "integer", "description": "Number of results (1-10)."},
                },
                "required": ["query"],
            },
        },
    },
]


class MemoryAgent:
    """Background subagent that extracts memory from recent conversation."""

    MAX_ITERATIONS = 10

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        model: str,
        embeddings_config: EmbeddingsConfig | None = None,
    ):
        self._provider = provider
        self._workspace = workspace
        self._model = model
        self._memory_dir = workspace / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)

        # Build memory_search index if embeddings available
        self._memory_index = None
        if (embeddings_config
                and embeddings_config.api_key
                and embeddings_config.api_base):
            from nano_alice.agent.tools.memory_search import _MemoryIndex
            self._memory_index = _MemoryIndex(
                memory_dir=self._memory_dir,
                api_base=embeddings_config.api_base,
                api_key=embeddings_config.api_key,
                model=embeddings_config.model,
                dimensions=embeddings_config.dimensions,
                extra_headers=embeddings_config.extra_headers,
            )

    async def run(self, recent_messages: list[dict]) -> None:
        """Extract memory from recent messages. Runs silently in background."""
        if not recent_messages:
            return

        # Format conversation for the subagent
        lines = []
        for m in recent_messages:
            content = m.get("content", "")
            if not content:
                continue
            role = m.get("role", "?").upper()
            ts = m.get("timestamp", "")[:16]
            prefix = f"[{ts}] " if ts else ""
            lines.append(f"{prefix}{role}: {content}")

        if not lines:
            return

        conversation_text = "\n".join(lines)
        system = _SYSTEM_PROMPT.format(memory_dir=self._memory_dir)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"## Recent Conversation\n\n{conversation_text}"},
        ]

        for iteration in range(self.MAX_ITERATIONS):
            try:
                response = await self._provider.chat(
                    messages=messages,
                    tools=_TOOLS,
                    model=self._model,
                )
            except Exception as e:
                logger.error("Memory agent LLM call failed (iter {}): {}", iteration, e)
                return

            if not response.has_tool_calls:
                logger.info("Memory agent done after {} iterations", iteration + 1)
                return

            # Record assistant message
            tool_call_dicts = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ]
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": tool_call_dicts,
            })

            # Execute each tool call
            for tc in response.tool_calls:
                logger.info("Memory agent tool: {}({})", tc.name,
                            json.dumps(tc.arguments, ensure_ascii=False)[:200])
                result = await self._execute_tool(tc.name, tc.arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": result,
                })

        logger.warning("Memory agent hit max iterations ({})", self.MAX_ITERATIONS)

    async def _execute_tool(self, name: str, args: dict) -> str:
        """Execute a tool call within the workspace."""
        try:
            if name == "read_file":
                return self._read_file(args.get("path", ""))
            elif name == "write_file":
                return self._write_file(args.get("path", ""), args.get("content", ""))
            elif name == "edit_file":
                return self._edit_file(
                    args.get("path", ""),
                    args.get("old_string", ""),
                    args.get("new_string", ""),
                )
            elif name == "list_dir":
                return self._list_dir(args.get("path", ""))
            elif name == "append_file":
                return self._append_file(args.get("path", ""), args.get("content", ""))
            elif name == "memory_search":
                return await self._memory_search(args.get("query", ""), args.get("top_k", 5))
            else:
                return f"Error: unknown tool '{name}'"
        except Exception as e:
            return f"Error: {e}"

    def _resolve(self, path: str) -> Path:
        """Resolve a path relative to workspace, ensuring it stays within."""
        resolved = (self._workspace / path).resolve()
        if not str(resolved).startswith(str(self._workspace.resolve())):
            raise ValueError("Path escapes workspace")
        return resolved

    _READ_LIMIT = 8000
    _HEAD_KEEP = 500  # preserve file header/structure

    def _read_file(self, path: str) -> str:
        fp = self._resolve(path)
        if not fp.exists():
            return f"File not found: {path}"
        text = fp.read_text(encoding="utf-8")
        if len(text) <= self._READ_LIMIT:
            return text
        # Large file: keep head (headers/structure) + tail (recent content)
        tail_keep = self._READ_LIMIT - self._HEAD_KEEP
        skipped = len(text) - self._READ_LIMIT
        return (
            text[:self._HEAD_KEEP]
            + f"\n\n... [{skipped} chars truncated, use memory_search for full content] ...\n\n"
            + text[-tail_keep:]
        )

    def _write_file(self, path: str, content: str) -> str:
        fp = self._resolve(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {path}"

    def _append_file(self, path: str, content: str) -> str:
        fp = self._resolve(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended {len(content)} chars to {path}"

    def _edit_file(self, path: str, old_string: str, new_string: str) -> str:
        fp = self._resolve(path)
        if not fp.exists():
            return f"File not found: {path}"
        text = fp.read_text(encoding="utf-8")
        if old_string not in text:
            return f"old_string not found in {path}"
        text = text.replace(old_string, new_string, 1)
        fp.write_text(text, encoding="utf-8")
        return f"Edited {path}"

    def _list_dir(self, path: str) -> str:
        dp = self._resolve(path)
        if not dp.is_dir():
            return f"Not a directory: {path}"
        entries = sorted(dp.iterdir())
        return "\n".join(
            f"{'[dir] ' if e.is_dir() else ''}{e.name}" for e in entries[:50]
        )

    async def _memory_search(self, query: str, top_k: int = 5) -> str:
        if not self._memory_index:
            return "Semantic search not available (no embeddings configured)."
        if not query:
            return "Error: query is required."
        top_k = max(1, min(10, int(top_k)))
        try:
            results = await self._memory_index.search(query, top_k)
        except Exception as e:
            return f"Error: search failed — {e}"
        if not results:
            return "No results found."
        lines = []
        for r in results:
            lines.append(f"[{r['score']}] {r['file']} (L{r['lines']})\n{r['text']}")
        return "\n\n---\n\n".join(lines)
