"""Semantic memory search tool using embeddings."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool


@dataclass
class _Chunk:
    source: str       # relative file path
    start_line: int
    end_line: int
    text: str


class _MemoryIndex:
    """Lazy-loaded, incrementally-updated embedding index stored as JSON."""

    def __init__(self, memory_dir: Path, api_base: str, api_key: str,
                 model: str, dimensions: int, extra_headers: dict[str, str]):
        self._memory_dir = memory_dir
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._extra_headers = extra_headers

        self._index_path = memory_dir / ".index.json"
        self._chunks: list[_Chunk] = []
        self._embeddings: list[list[float]] = []
        self._file_mtimes: dict[str, float] = {}
        self._config_hash = f"{model}:{dimensions}"
        self._loaded = False

    # --- persistence ---

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._index_path.exists():
            return
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            if data.get("config_hash") != self._config_hash:
                logger.info("Embedding config changed, rebuilding index")
                return
            self._file_mtimes = data.get("file_mtimes", {})
            for entry in data.get("chunks", []):
                self._chunks.append(_Chunk(
                    source=entry["source"],
                    start_line=entry["start_line"],
                    end_line=entry["end_line"],
                    text=entry["text"],
                ))
                self._embeddings.append(entry["embedding"])
        except Exception as e:
            logger.warning("Failed to load memory index: {}", e)
            self._chunks.clear()
            self._embeddings.clear()
            self._file_mtimes.clear()

    def _save(self) -> None:
        entries = []
        for chunk, emb in zip(self._chunks, self._embeddings):
            entries.append({
                "source": chunk.source,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "text": chunk.text,
                "embedding": emb,
            })
        data = {
            "config_hash": self._config_hash,
            "file_mtimes": self._file_mtimes,
            "chunks": entries,
        }
        self._index_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # --- chunking ---

    @staticmethod
    def _chunk_markdown(text: str, max_chars: int = 800) -> list[tuple[int, int, str]]:
        """Split markdown into chunks by headings and paragraphs.

        Returns list of (start_line, end_line, text).
        """
        lines = text.split("\n")
        chunks: list[tuple[int, int, str]] = []
        current_lines: list[str] = []
        start = 1

        def _flush():
            nonlocal current_lines, start
            if not current_lines:
                return
            block = "\n".join(current_lines).strip()
            if block:
                chunks.append((start, start + len(current_lines) - 1, block))
            start = start + len(current_lines)
            current_lines = []

        for i, line in enumerate(lines, 1):
            is_heading = bool(re.match(r"^#{1,3}\s", line))
            if is_heading and current_lines:
                _flush()
                start = i

            current_lines.append(line)
            current_text = "\n".join(current_lines)

            if len(current_text) >= max_chars:
                _flush()
                start = i + 1

        _flush()
        return chunks

    # --- embedding ---

    async def _batch_embed(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        import httpx

        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "curl/8.0",
                **self._extra_headers,
            }
            body: dict[str, Any] = {"model": self._model, "input": batch}
            if self._dimensions > 0:
                body["dimensions"] = self._dimensions

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self._api_base}/v1/embeddings",
                    headers=headers,
                    json=body,
                )
                resp.raise_for_status()
                result = resp.json()

            sorted_data = sorted(result["data"], key=lambda d: d["index"])
            all_embeddings.extend(d["embedding"] for d in sorted_data)

        return all_embeddings

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    # --- update & search ---

    async def _update(self) -> None:
        """Check for changed files and re-embed only dirty chunks."""
        self._load()

        md_files: dict[str, float] = {}
        for f in self._memory_dir.glob("**/*.md"):
            rel = str(f.relative_to(self._memory_dir))
            md_files[rel] = f.stat().st_mtime

        dirty_files: set[str] = set()
        for rel, mtime in md_files.items():
            if rel not in self._file_mtimes or self._file_mtimes[rel] < mtime:
                dirty_files.add(rel)

        removed_files = set(self._file_mtimes) - set(md_files)
        invalidated = dirty_files | removed_files

        if not invalidated:
            return

        # Remove old chunks for invalidated files
        keep = [
            (c, e) for c, e in zip(self._chunks, self._embeddings)
            if c.source not in invalidated
        ]
        if keep:
            self._chunks, self._embeddings = list(zip(*keep))
            self._chunks = list(self._chunks)
            self._embeddings = list(self._embeddings)
        else:
            self._chunks = []
            self._embeddings = []

        for rel in removed_files:
            self._file_mtimes.pop(rel, None)

        # Chunk and embed dirty files
        new_chunks: list[_Chunk] = []
        for rel in dirty_files:
            fp = self._memory_dir / rel
            if not fp.exists():
                continue
            text = fp.read_text(encoding="utf-8")
            for start, end, block in self._chunk_markdown(text):
                new_chunks.append(_Chunk(source=rel, start_line=start, end_line=end, text=block))

        if new_chunks:
            texts = [c.text for c in new_chunks]
            try:
                new_embeddings = await self._batch_embed(texts)
            except Exception as e:
                logger.error("Embedding API error: {}", e)
                return
            self._chunks.extend(new_chunks)
            self._embeddings.extend(new_embeddings)

        for rel in dirty_files:
            if rel in md_files:
                self._file_mtimes[rel] = md_files[rel]

        self._save()
        logger.info(
            "Memory index updated: {} chunks ({} files changed, {} removed)",
            len(self._chunks), len(dirty_files), len(removed_files),
        )

    async def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        await self._update()

        if not self._chunks:
            return []

        try:
            query_emb = (await self._batch_embed([query]))[0]
        except Exception as e:
            logger.error("Embedding API error during query: {}", e)
            return []

        scored = []
        for i, (chunk, emb) in enumerate(zip(self._chunks, self._embeddings)):
            sim = self._cosine_similarity(query_emb, emb)
            scored.append((sim, i, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        for sim, _, chunk in scored[:top_k]:
            results.append({
                "file": f"memory/{chunk.source}",
                "lines": f"{chunk.start_line}-{chunk.end_line}",
                "score": round(sim, 4),
                "text": chunk.text[:500],
            })
        return results


class MemorySearchTool(Tool):
    """Semantic search over memory files using embeddings."""

    def __init__(self, workspace: Path, api_base: str, api_key: str,
                 model: str, dimensions: int = 0,
                 extra_headers: dict[str, str] | None = None):
        self._index = _MemoryIndex(
            memory_dir=workspace / "memory",
            api_base=api_base,
            api_key=api_key,
            model=model,
            dimensions=dimensions,
            extra_headers=extra_headers or {},
        )

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search memory files by meaning (semantic search). "
            "Returns the most relevant chunks from MEMORY.md, HISTORY.md, daily logs, "
            "and other memory files. Use this when grep keyword search is insufficient."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query describing what to recall.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (1-10, default 5).",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query", "")
        top_k = kwargs.get("top_k", 5)
        if not query:
            return "Error: query is required."
        top_k = max(1, min(10, int(top_k)))

        try:
            results = await self._index.search(query, top_k)
        except Exception as e:
            return f"Error: search failed — {e}"

        if not results:
            return "No results found."

        lines = []
        for r in results:
            lines.append(
                f"[{r['score']}] {r['file']} (L{r['lines']})\n{r['text']}"
            )
        return "\n\n---\n\n".join(lines)
