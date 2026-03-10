"""System-side daily cache for same-day tool-result reuse."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nano_alice.logging_utils import json_ready, preview_value

_ALLOWED_SOURCE_TYPES = {
    "web_search",
    "web_fetch",
    "exec",
    "write_file",
    "edit_file",
    "spawn",
}


@dataclass
class DailyCacheRecord:
    """A structured same-day cache record stored in markdown."""

    timestamp: str
    session_key: str
    source_type: str
    tags: list[str]
    trigger: str
    input: str
    brief_summary: str
    reuse_note: str
    freshness: str
    links: list[str]
    paths: list[str]


class DailyCacheStore:
    """Append-only markdown store for daily cache records."""

    def __init__(self, workspace: Path):
        self._daily_dir = workspace / "memory" / "daily"
        self._daily_dir.mkdir(parents=True, exist_ok=True)

    def today_path(self, now: datetime | None = None) -> Path:
        stamp = (now or datetime.now()).date().isoformat()
        return self._daily_dir / f"{stamp}.md"

    def append_records(self, records: list[DailyCacheRecord], now: datetime | None = None) -> Path | None:
        if not records:
            return None
        path = self.today_path(now=now)
        if not path.exists():
            header = f"# Daily Cache - {path.stem}\n\n## Records\n\n"
            path.write_text(header, encoding="utf-8")

        with open(path, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(self._render_record(record))
        return path

    def load_today_records(
        self,
        now: datetime | None = None,
        *,
        session_key: str | None = None,
    ) -> list[DailyCacheRecord]:
        path = self.today_path(now=now)
        if not path.exists():
            return []
        records = self._parse_records(path.read_text(encoding="utf-8"))
        if session_key is None:
            return records
        return [record for record in records if record.session_key == session_key]

    @staticmethod
    def _render_record(record: DailyCacheRecord) -> str:
        lines = [
            f"### [{record.timestamp}] {record.source_type}",
            f"- session_key: {record.session_key}",
            f"- tags: [{', '.join(record.tags)}]",
            f"- source_type: {record.source_type}",
            f"- trigger: {record.trigger}",
            f"- input: {record.input}",
            f"- brief_summary: {record.brief_summary}",
            f"- reuse_note: {record.reuse_note}",
            f"- freshness: {record.freshness}",
        ]
        if record.links:
            lines.append("- links:")
            lines.extend(f"  - {link}" for link in record.links[:3])
        if record.paths:
            lines.append("- paths:")
            lines.extend(f"  - {path}" for path in record.paths[:3])
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _parse_records(content: str) -> list[DailyCacheRecord]:
        chunks = re.split(r"(?=^### \[)", content, flags=re.MULTILINE)
        records: list[DailyCacheRecord] = []
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk.startswith("### ["):
                continue
            lines = chunk.splitlines()
            header = lines[0]
            match = re.match(r"^### \[(?P<ts>[^\]]+)\] (?P<source>[a-z_]+)$", header)
            if not match:
                continue
            data: dict[str, Any] = {
                "timestamp": match.group("ts"),
                "session_key": "",
                "source_type": match.group("source"),
                "tags": [],
                "trigger": "",
                "input": "",
                "brief_summary": "",
                "reuse_note": "",
                "freshness": "session",
                "links": [],
                "paths": [],
            }
            current_list: str | None = None
            for raw_line in lines[1:]:
                line = raw_line.rstrip()
                stripped = line.strip()
                if stripped.startswith("- session_key: "):
                    data["session_key"] = stripped[len("- session_key: "):].strip()
                    current_list = None
                    continue
                if stripped.startswith("- tags: [") and stripped.endswith("]"):
                    body = stripped[len("- tags: ["):-1]
                    data["tags"] = [part.strip() for part in body.split(",") if part.strip()]
                    current_list = None
                    continue
                if stripped.startswith("- source_type: "):
                    data["source_type"] = stripped[len("- source_type: "):].strip()
                    current_list = None
                    continue
                if stripped.startswith("- trigger: "):
                    data["trigger"] = stripped[len("- trigger: "):].strip()
                    current_list = None
                    continue
                if stripped.startswith("- input: "):
                    data["input"] = stripped[len("- input: "):].strip()
                    current_list = None
                    continue
                if stripped.startswith("- brief_summary: "):
                    data["brief_summary"] = stripped[len("- brief_summary: "):].strip()
                    current_list = None
                    continue
                if stripped.startswith("- reuse_note: "):
                    data["reuse_note"] = stripped[len("- reuse_note: "):].strip()
                    current_list = None
                    continue
                if stripped.startswith("- freshness: "):
                    data["freshness"] = stripped[len("- freshness: "):].strip()
                    current_list = None
                    continue
                if stripped == "- links:":
                    current_list = "links"
                    continue
                if stripped == "- paths:":
                    current_list = "paths"
                    continue
                if current_list and stripped.startswith("- "):
                    data[current_list].append(stripped[2:].strip())
            if (
                not data["brief_summary"]
                or not data["session_key"]
                or data["source_type"] not in _ALLOWED_SOURCE_TYPES
            ):
                continue
            records.append(DailyCacheRecord(**data))
        return records


class DailyCacheRecorder:
    """Create high-value daily cache records from tool events."""

    def __init__(self, max_summary_chars: int = 140):
        self._max_summary_chars = max_summary_chars

    def build_records(
        self,
        *,
        session_key: str,
        trigger: str,
        tool_events: list[dict[str, Any]],
        final_content: str | None,
        now: datetime | None = None,
    ) -> list[DailyCacheRecord]:
        del final_content
        ts = (now or datetime.now()).strftime("%H:%M:%S")
        deduped = self._dedupe_events(tool_events)
        records: list[DailyCacheRecord] = []
        for event in deduped:
            record = self._build_record_for_event(
                ts=ts,
                session_key=session_key,
                trigger=trigger,
                event=event,
            )
            if record is not None:
                records.append(record)
        return records

    def _dedupe_events(self, tool_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        ordered: list[tuple[str, str]] = []
        for event in tool_events:
            name = str(event.get("name") or "").strip()
            if name not in _ALLOWED_SOURCE_TYPES:
                continue
            key = (name, self._signature_for_event(event))
            if key not in latest:
                ordered.append(key)
            latest[key] = event
        return [latest[key] for key in ordered]

    def _signature_for_event(self, event: dict[str, Any]) -> str:
        name = str(event.get("name") or "")
        args = event.get("arguments") or {}
        if name in {"web_search", "web_fetch"}:
            return str(args.get("query") or args.get("url") or "")
        if name in {"write_file", "edit_file"}:
            return str(args.get("path") or "")
        if name == "exec":
            return str(args.get("command") or args.get("cmd") or "")
        if name == "spawn":
            return str(args.get("task") or args.get("label") or "")
        return json.dumps(json_ready(args), ensure_ascii=False, sort_keys=True)

    def _build_record_for_event(
        self,
        *,
        ts: str,
        session_key: str,
        trigger: str,
        event: dict[str, Any],
    ) -> DailyCacheRecord | None:
        name = str(event.get("name") or "")
        args = event.get("arguments") or {}
        raw_result = event.get("result")
        result_text = raw_result if isinstance(raw_result, str) else json.dumps(json_ready(raw_result), ensure_ascii=False)

        if name == "web_search":
            return self._build_web_search_record(ts, session_key, trigger, args, result_text)
        if name == "web_fetch":
            return self._build_web_fetch_record(ts, session_key, trigger, args, result_text)
        if name == "exec":
            return self._build_exec_record(ts, session_key, trigger, args, result_text)
        if name in {"write_file", "edit_file"}:
            return self._build_file_edit_record(ts, session_key, trigger, name, args, result_text)
        if name == "spawn":
            return self._build_spawn_record(ts, session_key, trigger, args, result_text)
        return None

    def _build_web_search_record(
        self,
        ts: str,
        session_key: str,
        trigger: str,
        args: dict[str, Any],
        result_text: str,
    ) -> DailyCacheRecord | None:
        query = self._single_line(str(args.get("query") or "")).strip()
        if not query:
            return None
        lines = [line.strip() for line in result_text.splitlines() if line.strip()]
        links = [line for line in lines if line.startswith("http")]
        snippets = [line for line in lines if line and not line.startswith("Results for:") and not line.startswith("http") and not re.match(r"^\d+\.", line)]
        summary = self._clip(
            snippets[0] if snippets else f"已搜索“{query}”，结果可供当天同主题追问复用。"
        )
        return DailyCacheRecord(
            timestamp=ts,
            session_key=session_key,
            source_type="web_search",
            tags=self._keyword_tags(f"{query} {summary}"),
            trigger=self._clip(self._single_line(trigger), 100),
            input=f'query="{query}"',
            brief_summary=summary,
            reuse_note="当天若继续追问同一主题，可先复用该结论；若问最新进展，再重新搜索。",
            freshness="volatile",
            links=links[:3],
            paths=[],
        )

    def _build_web_fetch_record(
        self,
        ts: str,
        session_key: str,
        trigger: str,
        args: dict[str, Any],
        result_text: str,
    ) -> DailyCacheRecord | None:
        url = self._single_line(str(args.get("url") or "")).strip()
        if not url:
            return None
        title = ""
        summary = ""
        links = [url]
        try:
            parsed = json.loads(result_text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            text = str(parsed.get("text") or "")
            title = self._extract_markdown_title(text)
            summary = self._clip(self._summary_from_text(text) or self._single_line(text))
            final_url = str(parsed.get("finalUrl") or url)
            if final_url:
                links = [final_url]
        if not summary:
            summary = f"已抓取页面 {title or url}，可复用其当天关键信息。"
        return DailyCacheRecord(
            timestamp=ts,
            session_key=session_key,
            source_type="web_fetch",
            tags=self._keyword_tags(f"{title} {url} {summary}"),
            trigger=self._clip(self._single_line(trigger), 100),
            input=f"url={url}",
            brief_summary=summary,
            reuse_note="当天若继续围绕该页面追问，可先复用摘要；若页面可能更新，再重新抓取。",
            freshness="volatile",
            links=links[:3],
            paths=[],
        )

    def _build_exec_record(
        self,
        ts: str,
        session_key: str,
        trigger: str,
        args: dict[str, Any],
        result_text: str,
    ) -> DailyCacheRecord | None:
        command = self._single_line(str(args.get("command") or args.get("cmd") or "")).strip()
        if not command:
            return None
        summary = self._clip(self._summary_from_text(result_text))
        if not summary:
            return None
        return DailyCacheRecord(
            timestamp=ts,
            session_key=session_key,
            source_type="exec",
            tags=self._keyword_tags(f"{command} {summary}"),
            trigger=self._clip(self._single_line(trigger), 100),
            input=command,
            brief_summary=summary,
            reuse_note="当天若只是复述这次执行结果，可先复用；若结果依赖实时状态，请重新执行。",
            freshness="volatile",
            links=[],
            paths=self._extract_paths(f"{command}\n{result_text}")[:3],
        )

    def _build_file_edit_record(
        self,
        ts: str,
        session_key: str,
        trigger: str,
        source_type: str,
        args: dict[str, Any],
        result_text: str,
    ) -> DailyCacheRecord | None:
        path = self._single_line(str(args.get("path") or "")).strip()
        if not path:
            return None
        summary = self._clip(
            self._summary_from_text(result_text) or f"已更新 {path}。"
        )
        return DailyCacheRecord(
            timestamp=ts,
            session_key=session_key,
            source_type=source_type,
            tags=self._keyword_tags(f"{path} {summary}"),
            trigger=self._clip(self._single_line(trigger), 100),
            input=f"path={path}",
            brief_summary=summary,
            reuse_note="当天若再次追问这次改动结果，可先复用；若文件已继续变化，应重新读取。",
            freshness="session",
            links=[],
            paths=[path],
        )

    def _build_spawn_record(
        self,
        ts: str,
        session_key: str,
        trigger: str,
        args: dict[str, Any],
        result_text: str,
    ) -> DailyCacheRecord | None:
        task = self._single_line(str(args.get("task") or args.get("label") or "")).strip()
        if not task:
            return None
        summary = self._clip(
            self._summary_from_text(result_text) or f"已创建后台任务：{task}。"
        )
        return DailyCacheRecord(
            timestamp=ts,
            session_key=session_key,
            source_type="spawn",
            tags=self._keyword_tags(f"{task} {summary}"),
            trigger=self._clip(self._single_line(trigger), 100),
            input=task,
            brief_summary=summary,
            reuse_note="当天若继续跟进同一后台任务，可先参考该记录，再决定是否等待新结果。",
            freshness="session",
            links=[],
            paths=[],
        )

    def _summary_from_text(self, text: str) -> str:
        lines = [self._single_line(line) for line in text.splitlines()]
        for line in lines:
            if not line:
                continue
            lowered = line.lower()
            if lowered.startswith("results for:"):
                continue
            if lowered.startswith("error:"):
                return line
            if line.startswith("http"):
                continue
            if re.match(r"^\d+\.", line):
                continue
            return line
        return self._single_line(text)

    @staticmethod
    def _extract_markdown_title(text: str) -> str:
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        if first_line.startswith("# "):
            return first_line[2:].strip()
        return ""

    @staticmethod
    def _extract_paths(text: str) -> list[str]:
        paths = re.findall(r"(?:^|[\s'\"])(/[^\s'\"]+|[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)", text)
        unique: list[str] = []
        for path in paths:
            if path not in unique:
                unique.append(path)
        return unique

    @staticmethod
    def _keyword_tags(text: str) -> list[str]:
        tokens = re.findall(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]{2,6}", text.lower())
        stopwords = {"the", "and", "for", "with", "from", "this", "that", "query", "path", "已", "当天", "继续", "结果"}
        tags: list[str] = []
        for token in tokens:
            cleaned = token.strip(" ./:-_")
            if not cleaned or cleaned in stopwords or cleaned.isdigit():
                continue
            if cleaned not in tags:
                tags.append(cleaned)
            if len(tags) >= 5:
                break
        return tags or ["daily-cache"]

    @staticmethod
    def _single_line(text: str) -> str:
        return " ".join(text.strip().split())

    def _clip(self, text: str, limit: int | None = None) -> str:
        clipped_limit = limit or self._max_summary_chars
        normalized = self._single_line(text)
        if len(normalized) <= clipped_limit:
            return normalized
        return normalized[:clipped_limit].rstrip() + "..."


class TodayRecallRetriever:
    """Retrieve the most relevant same-day cache records for the current query."""

    def __init__(self, store: DailyCacheStore, top_k: int = 3):
        self._store = store
        self._top_k = top_k

    def recall(
        self,
        query: str,
        now: datetime | None = None,
        *,
        session_key: str | None = None,
    ) -> str | None:
        records = self._store.load_today_records(now=now, session_key=session_key)
        if not records:
            return None
        ranked = self._rank(query, records)
        if not ranked:
            return None
        lines: list[str] = []
        for record in ranked[: self._top_k]:
            link_blob = ""
            if record.links:
                link_blob = f" | links: {', '.join(record.links[:2])}"
            path_blob = ""
            if record.paths:
                path_blob = f" | paths: {', '.join(record.paths[:2])}"
            lines.append(
                f"[{record.timestamp}] {record.source_type}: {record.brief_summary} "
                f"Reuse: {record.reuse_note}{link_blob}{path_blob}"
            )
        return "\n".join(lines)

    def _rank(self, query: str, records: list[DailyCacheRecord]) -> list[DailyCacheRecord]:
        query_terms = set(self._terms(query))
        if not query_terms:
            return []
        scored: list[tuple[int, datetime, DailyCacheRecord]] = []
        for record in records:
            haystack_terms = set(
                self._terms(
                    " ".join(
                        [
                            record.source_type,
                            " ".join(record.tags),
                            record.input,
                            record.brief_summary,
                            record.reuse_note,
                            " ".join(record.links),
                            " ".join(record.paths),
                        ]
                    )
                )
            )
            overlap = query_terms & haystack_terms
            if not overlap:
                continue
            score = len(overlap) * 3
            if any(term in record.tags for term in query_terms):
                score += 2
            if any(term in record.input.lower() for term in query_terms):
                score += 2
            if any(term in record.brief_summary.lower() for term in query_terms):
                score += 2
            scored.append((score, self._parse_timestamp(record.timestamp), record))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [record for _, _, record in scored]

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        try:
            return datetime.strptime(value, "%H:%M:%S")
        except ValueError:
            return datetime.min

    @staticmethod
    def _terms(text: str) -> list[str]:
        tokens = re.findall(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]{2,6}", text.lower())
        stopwords = {
            "今天",
            "一下",
            "看看",
            "确认",
            "再次",
            "还有没有",
            "是不是",
            "一下子",
            "请问",
        }
        terms = [token.strip(" ./:-_") for token in tokens if token.strip(" ./:-_")]
        return [term for term in terms if term not in stopwords]
