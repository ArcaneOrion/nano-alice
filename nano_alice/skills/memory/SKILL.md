---
name: memory
description: Passive memory management with RAG recall and background extraction.
always: true
---

# Memory

## How It Works

Memory is **fully automatic** — you do NOT need to manage it yourself.

- **RAG Recall**: Each turn, relevant memories are automatically retrieved via semantic search and injected into your context as "Recalled Context". You don't need to search for them.
- **Memory Subagent**: After each conversation turn, a background agent extracts facts, decisions, and preferences from the conversation and writes them to memory files.
- **Consolidation**: Old messages are automatically trimmed when the session grows large.

## Structure

| File | Purpose | Auto-managed? |
|------|---------|---------------|
| `memory/MEMORY.md` | Long-term facts, preferences, project overview | Yes (subagent) |
| `memory/HISTORY.md` | Append-only event log for important events and confirmations | Yes (subagent) |
| `memory/SCRATCH.md` | Per-turn conversation summaries (subagent scratchpad) | Yes (subagent) |
| `memory/YYYY-MM-DD.md` | Daily logs | No (not in current auto-write path) |
| `memory/projects.md` | Active project status and todos | Yes (subagent) |
| `memory/lessons.md` | Lessons learned, mistakes to avoid | Yes (subagent) |

Current auto-management is intentionally conservative:
- `memory/MEMORY.md` is for stable long-term facts only.
- `memory/projects.md` is the default home for active project status changes.
- `memory/lessons.md` is for reusable lessons, not one-off incidents.
- `memory/HISTORY.md` records important events worth future recall.
- `memory/SCRATCH.md` is short-term scratch memory.
- Daily logs are not part of the current automatic write path.

## When YOU Should Act

Only write to memory files when the **user explicitly asks** you to remember something. For example:
- "Remember that I prefer dark mode"
- "Save this decision to memory"

For normal conversation, just respond naturally. The memory subagent handles everything in the background.

## Search

Use `memory_search` for **active deep recall** when you need specific past information:
  memory_search(query="nginx deployment issue", top_k=5)

Use `exec` with grep for exact keyword search:
  exec(command="grep -i 'nginx' memory/HISTORY.md")
