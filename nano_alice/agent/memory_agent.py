"""Background memory subagent that extracts info from recent conversations."""

from __future__ import annotations

import json
import re
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
- **SCRATCH.md is append-only**: do NOT read it before appending. Recent SCRATCH duplicate checks \
are handled automatically.
- **Stay in scope**: manage only the standard memory files. Do NOT create daily logs or ad-hoc files.
- **Skip trivial conversations**: greetings, small talk, "1+1=?" → do NOT write anything, \
just reply with a summary and STOP.
- **Large files are tail-truncated**: read_file shows the header + the most recent content. \
If you need older content, use memory_search.
- **Finish quickly**: aim for ≤5 tool-call rounds. Stop as soon as writes are done.

## Instructions

### 1. Decide if anything is worth recording
Read the conversation. The input has two sections:
- **Context** — already processed messages, for background understanding only. Do NOT extract from these.
- **New Conversation** — unprocessed messages. Extract new information ONLY from this section.
If the new conversation is only greetings/small talk/trivial Q&A, reply "Nothing notable" \
and STOP (zero tool calls).

Only write memory when at least one of these is true:
- The user explicitly asked you to remember something.
- A long-term preference, constraint, or stable fact was added or changed.
- An active project status, blocker, decision, or next step changed.
- A reusable lesson or rule was learned.
- An important event happened that may need to be recalled later.

Skip writing for:
- greetings / pleasantries / self-introductions
- one-off factual Q&A with no future value
- short acknowledgements or polite follow-ups
- temporary chatter that does not change preferences, projects, or lessons

### 2. Check for duplicates
Use memory_search (1-2 short queries, 5-10 words each) to see if the key facts already exist \
in long-term memory or managed memory files. If they do, skip writing. Do NOT search more than twice.
For `memory/SCRATCH.md`, recent-entry duplicate checks are handled automatically when appending.

### 3. Write to the right file
**MEMORY.md（主文件，≤5KB，每轮全量注入 system prompt）**
- Core user facts, preferences, capabilities summary, file index table.
- Only add truly new long-term facts here. Default to NOT writing this file unless the fact is stable.

**HISTORY.md（关键事件流，append-only）**
- Important events, confirmations, failures, reversals, or system-triggered outcomes worth time-based recall.
- Do not write routine chatter here.

**Sub-files（通过 RAG 按需召回）**
- `memory/schedule.md` — course schedule, class times
- `memory/projects.md` — active project status and TODOs
- `memory/lessons.md` — lessons learned, mistakes to avoid
- `memory/SCRATCH.md` — timestamped conversation summaries (append-only!)
- Do NOT create other topic files in this task.

Routing rules:
- stable preference / long-term fact → `memory/MEMORY.md`
- active project progress / blocker / next step / delivery state → `memory/projects.md`
- reusable lesson / mistake to avoid / operating rule → `memory/lessons.md`
- important event / confirmation / failure / system event → `memory/HISTORY.md`
- short-term conversation summary → `memory/SCRATCH.md`

Update rules:
- For `memory/projects.md`, `memory/lessons.md`, and `memory/MEMORY.md`, prefer updating existing entries over appending duplicates.
- For `memory/HISTORY.md` and `memory/SCRATCH.md`, append-only is fine.
- Do NOT write `memory/YYYY-MM-DD.md` in this task.

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

_MAINTENANCE_SYSTEM_PROMPT = """\
You are a memory maintenance agent. Reconcile and clean existing memory files.

## Goal
- Review the existing standard memory files and improve consistency.
- Fix likely hallucinations or wrong placements that may have been written previously.
- Keep the current file scope and responsibilities.

## Hard rules
- Do NOT invent any new facts.
- Only use facts already present in the existing memory files.
- If a statement is uncertain, contradictory, or looks hallucinated, prefer removing it from long-term files or downgrading it rather than strengthening it.
- Stay within the managed files. Do NOT create daily logs or ad-hoc files.
- Keep changes minimal and conservative.

## File responsibilities
- `memory/MEMORY.md`: stable long-term facts and preferences only.
- `memory/projects.md`: current active project status, blockers, next steps, delivery state.
- `memory/lessons.md`: reusable lessons and rules only.
- `memory/HISTORY.md`: important events worth later recall.
- `memory/SCRATCH.md`: short-term summaries; can be compacted.
- `memory/schedule.md`: schedules and recurring time-based information.

## Maintenance tasks
1. Read all listed managed files that exist.
2. Remove duplicates and merge repeated statements.
3. Fix obvious file misplacements (for example, move project status out of MEMORY.md into projects.md).
4. Resolve contradictions conservatively using the most recent or most clearly supported wording already present in the files.
5. Keep `MEMORY.md` concise and stable.
6. Avoid broad rewrites when no real cleanup is needed.

## Write policy
- `memory/MEMORY.md`, `memory/projects.md`, `memory/lessons.md`, `memory/schedule.md`: prefer targeted updates or full rewrites only when needed.
- `memory/HISTORY.md` and `memory/SCRATCH.md`: maintenance mode may rewrite these files to remove duplication or compress noise.

## Stop condition
If no meaningful cleanup is needed, reply with a one-line summary and make zero writes.
After writes are complete, reply with a one-line summary and stop."""

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
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to workspace.",
                    },
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
    _MANAGED_MEMORY_FILES = {
        "memory/MEMORY.md",
        "memory/HISTORY.md",
        "memory/SCRATCH.md",
        "memory/projects.md",
        "memory/lessons.md",
        "memory/schedule.md",
    }
    _APPEND_ONLY_FILES = {"memory/HISTORY.md", "memory/SCRATCH.md"}
    _UPDATE_PREFERRED_FILES = {"memory/MEMORY.md", "memory/projects.md", "memory/lessons.md"}
    _DAILY_LOG_RE = re.compile(r"^memory/\d{4}-\d{2}-\d{2}\.md$")
    _SCRATCH_ENTRY_RE = re.compile(r"^### \[[^\]]+\].*$", re.MULTILINE)
    _SCRATCH_DEDUP_RECENT = 8
    _SCRATCH_TAIL_CHARS = 6000
    _MAINTENANCE_TARGET_FILES = [
        "memory/MEMORY.md",
        "memory/HISTORY.md",
        "memory/SCRATCH.md",
        "memory/projects.md",
        "memory/lessons.md",
        "memory/schedule.md",
    ]

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
        self._allow_scratch_overwrite = False
        self._maintenance_mode = False

        # Build memory_search index if embeddings available
        self._memory_index = None
        if embeddings_config and embeddings_config.api_key and embeddings_config.api_base:
            from nano_alice.agent.tools.memory_search import _MemoryIndex

            self._memory_index = _MemoryIndex(
                memory_dir=self._memory_dir,
                api_base=embeddings_config.api_base,
                api_key=embeddings_config.api_key,
                model=embeddings_config.model,
                dimensions=embeddings_config.dimensions,
                extra_headers=embeddings_config.extra_headers,
            )

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        """Format a list of messages into readable text."""
        lines = []
        for m in messages:
            content = m.get("content", "")
            if not content:
                continue
            role = m.get("role", "?").upper()
            ts = m.get("timestamp", "")[:16]
            prefix = f"[{ts}] " if ts else ""
            lines.append(f"{prefix}{role}: {content}")
        return "\n".join(lines)

    async def run(
        self,
        new_messages: list[dict],
        context_messages: list[dict] | None = None,
        cleanup_scratch: bool = False,
        memory_priority: str = "normal",
        pre_search_results: list[dict] | None = None,
    ) -> None:
        """Extract memory from recent messages. Runs silently in background.

        Args:
            new_messages: Messages that haven't been processed yet (extract from these).
            context_messages: Optional older messages for background understanding (read-only).
            cleanup_scratch: If True, also clean up SCRATCH.md (compress entries older than 48h).
            memory_priority: "high" if user explicitly asked to remember something.
            pre_search_results: RAG search results to avoid duplicate embedding calls.
        """
        if not new_messages:
            return

        # Format context section (already processed, for reference only)
        sections: list[str] = []
        if context_messages:
            ctx_lines = self._format_messages(context_messages)
            if ctx_lines:
                sections.append(
                    "## Context (already processed, for reference only)\n\n" + ctx_lines
                )

        # Format new conversation section (extract from this)
        new_lines = self._format_messages(new_messages)
        if not new_lines:
            return

        new_header = "## New Conversation (extract from this)\n"
        if memory_priority == "high":
            new_header += (
                "\n**PRIORITY: HIGH** — 用户明确要求记住某些信息，务必写入记忆文件，不要跳过。\n"
            )
        sections.append(new_header + "\n" + new_lines)

        conversation_text = "\n\n".join(sections)
        system = _SYSTEM_PROMPT.format(memory_dir=self._memory_dir)

        # 如果有预搜索结果，注入到 system prompt 作为已有记忆参考（去重检查）
        if pre_search_results:
            existing = "\n\n".join(
                f"[{r['file']} L{r['lines']}] {r['text']}" for r in pre_search_results
            )
            system += f"""

## Existing Memory (for deduplication check)

The following information already exists in memory:

{existing}

Before writing new information, check if these facts already exist. Update existing entries instead of duplicating.
"""

        if cleanup_scratch:
            self._allow_scratch_overwrite = True
            system += (
                "\n\n## SCRATCH.md Cleanup Task\n"
                "SCRATCH.md hasn't been cleaned in over 48 hours. After processing new messages:\n"
                "1. Read memory/SCRATCH.md\n"
                "2. Entries older than 48 hours → compress into a brief monthly summary "
                "(one `### [YYYY-MM] Month Summary` section per month)\n"
                "3. Keep entries from the last 48 hours as-is\n"
                "4. Use write_file to write the cleaned version back\n"
                "If SCRATCH.md doesn't exist or is small (<1KB), skip cleanup."
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": conversation_text},
        ]

        try:
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
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": tool_call_dicts,
                    }
                )

                # Execute each tool call
                for tc in response.tool_calls:
                    logger.info(
                        "Memory agent tool: {}({})",
                        tc.name,
                        json.dumps(tc.arguments, ensure_ascii=False)[:200],
                    )
                    result = await self._execute_tool(tc.name, tc.arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": result,
                        }
                    )

            logger.warning("Memory agent hit max iterations ({})", self.MAX_ITERATIONS)
        finally:
            self._allow_scratch_overwrite = False

    async def run_maintenance(self) -> str:
        """Reconcile existing managed memory files."""
        listed_files = []
        for rel in self._MAINTENANCE_TARGET_FILES:
            if self._resolve(rel).exists():
                listed_files.append(rel)

        if not listed_files:
            logger.info("Memory maintenance skipped: no managed memory files found")
            return "No managed memory files found."

        user_prompt = (
            "## Managed Files\n"
            + "\n".join(f"- {path}" for path in listed_files)
            + "\n\nRead the existing files, reconcile them conservatively, and only write if cleanup is truly needed."
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _MAINTENANCE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        self._maintenance_mode = True
        self._allow_scratch_overwrite = True
        final_summary = "Memory maintenance finished."
        try:
            for iteration in range(self.MAX_ITERATIONS):
                try:
                    response = await self._provider.chat(
                        messages=messages,
                        tools=_TOOLS,
                        model=self._model,
                    )
                except Exception as e:
                    logger.error("Memory maintenance LLM call failed (iter {}): {}", iteration, e)
                    return f"Error: {e}"

                if not response.has_tool_calls:
                    final_summary = response.content or final_summary
                    logger.info("Memory maintenance done after {} iterations", iteration + 1)
                    return final_summary

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
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": tool_call_dicts,
                    }
                )

                for tc in response.tool_calls:
                    logger.info(
                        "Memory maintenance tool: {}({})",
                        tc.name,
                        json.dumps(tc.arguments, ensure_ascii=False)[:200],
                    )
                    result = await self._execute_tool(tc.name, tc.arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": result,
                        }
                    )

            logger.warning("Memory maintenance hit max iterations ({})", self.MAX_ITERATIONS)
            return final_summary
        finally:
            self._maintenance_mode = False
            self._allow_scratch_overwrite = False

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

    @staticmethod
    def _normalize_memory_path(path: str) -> str:
        return Path(path).as_posix().lstrip("./")

    def _validate_write_operation(self, op: str, path: str) -> str | None:
        normalized = self._normalize_memory_path(path)

        if self._DAILY_LOG_RE.match(normalized):
            return "Error: daily log writes are out of scope"

        if normalized.startswith("memory/"):
            if normalized.endswith(".md") and normalized not in self._MANAGED_MEMORY_FILES:
                return f"Error: unmanaged memory file '{normalized}'"
            if normalized not in self._MANAGED_MEMORY_FILES:
                return f"Error: writes are only allowed for managed memory files, got '{normalized}'"

        if normalized == "memory/SCRATCH.md" and op != "append_file" and not self._allow_scratch_overwrite:
            return "Error: SCRATCH.md is append-only outside cleanup"

        if (
            normalized in self._APPEND_ONLY_FILES
            and op == "edit_file"
            and not self._maintenance_mode
        ):
            return f"Error: {normalized} is append-only"

        if (
            normalized == "memory/HISTORY.md"
            and op == "write_file"
            and not self._maintenance_mode
        ):
            return "Error: use append_file for memory/HISTORY.md outside maintenance"

        if normalized in self._UPDATE_PREFERRED_FILES and op == "append_file":
            return f"Error: use edit_file or write_file for {normalized}"

        if normalized.endswith(".md") and not normalized.startswith("memory/"):
            return f"Error: writes must stay within memory/, got '{normalized}'"

        return None

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
            text[: self._HEAD_KEEP]
            + f"\n\n... [{skipped} chars truncated, use memory_search for full content] ...\n\n"
            + text[-tail_keep:]
        )

    def _write_file(self, path: str, content: str) -> str:
        if error := self._validate_write_operation("write_file", path):
            return error
        fp = self._resolve(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {path}"

    def _append_file(self, path: str, content: str) -> str:
        if error := self._validate_write_operation("append_file", path):
            return error
        normalized = self._normalize_memory_path(path)
        if normalized == "memory/SCRATCH.md" and self._is_duplicate_scratch_entry(content):
            return "Skipped duplicate SCRATCH.md entry"
        fp = self._resolve(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended {len(content)} chars to {path}"

    @classmethod
    def _split_scratch_entries(cls, text: str) -> list[str]:
        matches = list(cls._SCRATCH_ENTRY_RE.finditer(text))
        if not matches:
            chunk = text.strip()
            return [chunk] if chunk else []

        entries: list[str] = []
        for index, match in enumerate(matches):
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            chunk = text[start:end].strip()
            if chunk:
                entries.append(chunk)
        return entries

    @classmethod
    def _normalize_scratch_entry(cls, text: str) -> str:
        lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("### ["):
                line = re.sub(r"^###\s*\[[^\]]+\]\s*", "", line)
            elif line.startswith(("- ", "* ")):
                line = line[2:].strip()
            line = re.sub(r"\s+", " ", line).strip().lower()
            if line:
                lines.append(line)
        return "\n".join(lines)

    @classmethod
    def _scratch_bullets(cls, text: str) -> tuple[str, tuple[str, ...]]:
        title = ""
        bullets: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("### ["):
                title = re.sub(r"^###\s*\[[^\]]+\]\s*", "", line)
                title = re.sub(r"\s+", " ", title).strip().lower()
                continue
            if line.startswith(("- ", "* ")):
                bullet = re.sub(r"\s+", " ", line[2:]).strip().lower()
                if bullet:
                    bullets.append(bullet)
        return title, tuple(bullets)

    @classmethod
    def _scratch_entries_look_similar(cls, candidate: str, existing: str) -> bool:
        if candidate == existing:
            return True

        candidate_title, candidate_bullets = cls._scratch_bullets(candidate)
        existing_title, existing_bullets = cls._scratch_bullets(existing)

        return bool(candidate_title) and candidate_title == existing_title and candidate_bullets == existing_bullets

    def _is_duplicate_scratch_entry(self, content: str) -> bool:
        candidate = self._normalize_scratch_entry(content)
        if not candidate:
            return False

        scratch_path = self._resolve("memory/SCRATCH.md")
        if not scratch_path.exists():
            return False

        existing_text = scratch_path.read_text(encoding="utf-8")
        tail = existing_text[-self._SCRATCH_TAIL_CHARS :]
        recent_entries = self._split_scratch_entries(tail)[-self._SCRATCH_DEDUP_RECENT :]
        for entry in recent_entries:
            normalized = self._normalize_scratch_entry(entry)
            if normalized and self._scratch_entries_look_similar(candidate, normalized):
                return True
        return False

    def _edit_file(self, path: str, old_string: str, new_string: str) -> str:
        if error := self._validate_write_operation("edit_file", path):
            return error
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
        return "\n".join(f"{'[dir] ' if e.is_dir() else ''}{e.name}" for e in entries[:50])

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
