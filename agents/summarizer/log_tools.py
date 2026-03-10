import os
import re
from collections import Counter, defaultdict
from pathlib import Path

MAX_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "5000"))
MAX_LINE_TEXT_CHARS = int(os.getenv("MAX_TOOL_LINE_TEXT_CHARS", "240"))
MAX_EXCERPT_LINES = int(os.getenv("MAX_EXCERPT_LINES", "120"))
MAX_REGEX_LENGTH = int(os.getenv("MAX_REGEX_LENGTH", "120"))

FILE_MARKER_RE = re.compile(r"^---\s+(.+?)\s+---\s*$")
AZURE_SECTION_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}T\S+\s+)?##\[section\]\s*(.+?)\s*$",
    re.IGNORECASE,
)
STACKTRACE_RE = re.compile(r"^\s+at\s+.+|^\s*File\s+\".+\", line \d+", re.IGNORECASE)
NESTED_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+\s+\d{4}-\d{2}-\d{2}T\S+")

FAILURE_PATTERNS = [
    ("azure_error", re.compile(r"##\[(error|fatal)\]", re.IGNORECASE)),
    (
        "azure_task_issue",
        re.compile(r"##vso\[task\.logissue\s+type=error", re.IGNORECASE),
    ),
    ("azure_warning", re.compile(r"##\[warning\]", re.IGNORECASE)),
    (
        "task_failed",
        re.compile(
            r"\b(task failed|job failed|build failed|deployment failed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "exit_code",
        re.compile(
            r"\b(exit code|exited with code|script failed with exit code)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "exception",
        re.compile(r"\b(exception|traceback|stack trace|fatal)\b", re.IGNORECASE),
    ),
    ("npm_error", re.compile(r"\bnpm ERR!\b", re.IGNORECASE)),
    (
        "dotnet_error",
        re.compile(r"\b(error\s+[A-Z]{2,}\d+|MSB\d+|NU\d+)\b", re.IGNORECASE),
    ),
    (
        "test_failure",
        re.compile(
            r"\b(test run failed|tests? failed|failing tests?)\b", re.IGNORECASE
        ),
    ),
    (
        "generic_error",
        re.compile(r"\blevel=error\b|^\[ERROR\]|\berror:", re.IGNORECASE),
    ),
    (
        "generic_warning",
        re.compile(r"\blevel=warn(?:ing)?\b|^\[WARN(?:ING)?\]", re.IGNORECASE),
    ),
]

REDACTION_PATTERNS = [
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|pat)\b"
            r"([=:]\s*)([^\s,;]+)"
        ),
        r"\1\2***",
    ),
    (
        re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._-]+)"),
        r"\1***",
    ),
    (
        re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
        "***",
    ),
]

SIGNATURE_CLEANUPS = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}[T ][^ ]+\s*"), ""),
    (re.compile(r"^##\[[a-z]+\]\s*", re.IGNORECASE), ""),
    (re.compile(r"^##vso\[[^\]]+\]\s*", re.IGNORECASE), ""),
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "<guid>",
    ),
    (re.compile(r"\breq[-_:][A-Za-z0-9._-]+\b", re.IGNORECASE), "req-<id>"),
    (re.compile(r"\b(job|run|build|attempt|task)[-_:][A-Za-z0-9._-]+\b", re.IGNORECASE), r"\1-<id>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"\b\d+\b"), "<num>"),
    (re.compile(r"\s+"), " "),
]


def clamp_int(value, default, min_value, max_value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(parsed, max_value))


def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


class LogTools:
    def __init__(self, log_file):
        self.log_file = Path(log_file)
        self.text = self.log_file.read_text(encoding="utf-8", errors="replace")
        self.lines = self.text.splitlines()
        self.file_size_bytes = self.log_file.stat().st_size
        self.scope_by_line = self._build_scope_index()

    def get_tool_schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_log_overview",
                    "description": (
                        "Get a compact overview of the log file without exposing "
                        "the entire log."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "How many sections or previews to return.",
                                "minimum": 1,
                                "maximum": 10,
                                "default": 5,
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_logs",
                    "description": (
                        "Search the log with a regex pattern and optionally include "
                        "a few surrounding lines."
                    ),
                    "parameters": {
                        "type": "object",
                        "required": ["pattern"],
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "Python regex pattern to search for.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 25,
                                "default": 10,
                            },
                            "before": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 5,
                                "default": 0,
                            },
                            "after": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 5,
                                "default": 0,
                            },
                            "case_sensitive": {
                                "type": "boolean",
                                "default": False,
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "find_failure_markers",
                    "description": (
                        "Find Azure DevOps failure markers, exit code lines, "
                        "exceptions, and similar pipeline failure signals."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 25,
                                "default": 10,
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "top_error_signatures",
                    "description": (
                        "Group similar error lines and return the most frequent "
                        "error signatures."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "top_k": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 10,
                                "default": 5,
                            },
                            "min_occurrences": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 10,
                                "default": 1,
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "likely_root_cause",
                    "description": (
                        "Score and rank the most likely root cause lines using "
                        "deterministic heuristics."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "top_k": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                                "default": 3,
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "error_timeline",
                    "description": (
                        "Return a compact timeline of warnings, errors, failures, "
                        "and section transitions."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 25,
                                "default": 12,
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_log_excerpt",
                    "description": (
                        "Read a bounded log excerpt by line numbers. Use this when "
                        "you need local context around a specific failure."
                    ),
                    "parameters": {
                        "type": "object",
                        "required": ["start_line", "end_line"],
                        "properties": {
                            "start_line": {"type": "integer", "minimum": 1},
                            "end_line": {"type": "integer", "minimum": 1},
                        },
                    },
                },
            },
        ]

    def execute(self, tool_name, arguments):
        safe_arguments = arguments or {}
        available = {
            "get_log_overview": self.get_log_overview,
            "search_logs": self.search_logs,
            "find_failure_markers": self.find_failure_markers,
            "top_error_signatures": self.top_error_signatures,
            "likely_root_cause": self.likely_root_cause,
            "error_timeline": self.error_timeline,
            "get_log_excerpt": self.get_log_excerpt,
        }
        if tool_name not in available:
            return self._error_payload(
                tool_name,
                "unknown_tool",
                f"Tool '{tool_name}' is not registered.",
            )

        try:
            return available[tool_name](**safe_arguments)
        except TypeError as exc:
            return self._error_payload(
                tool_name,
                "invalid_arguments",
                f"Invalid arguments for '{tool_name}': {exc}",
            )
        except Exception as exc:
            return self._error_payload(
                tool_name,
                "tool_execution_failed",
                f"Tool '{tool_name}' failed: {exc}",
            )

    def get_log_overview(self, limit=5):
        preview_limit = clamp_int(limit, 5, 1, 10)
        counts = Counter()
        sections = []
        seen_sections = set()

        for index, line in enumerate(self.lines):
            marker = self._classify_marker(line)
            if marker:
                counts[marker] += 1

            scope = self.scope_by_line[index]
            if scope not in seen_sections:
                seen_sections.add(scope)
                sections.append(scope)

        return self._finalize_payload(
            {
                "ok": True,
                "tool": "get_log_overview",
                "log_file": str(self.log_file),
                "line_count": len(self.lines),
                "file_size_bytes": self.file_size_bytes,
                "marker_counts": dict(counts),
                "sections": sections[:preview_limit],
                "tail_preview": self._tail_preview(preview_limit),
            }
        )

    def search_logs(self, pattern, limit=10, before=0, after=0, case_sensitive=False):
        if not isinstance(pattern, str) or not pattern.strip():
            return self._error_payload(
                "search_logs",
                "invalid_pattern",
                "pattern must be a non-empty string",
            )
        if len(pattern) > MAX_REGEX_LENGTH:
            return self._error_payload(
                "search_logs",
                "pattern_too_long",
                f"pattern exceeds the limit of {MAX_REGEX_LENGTH} characters",
            )

        match_limit = clamp_int(limit, 10, 1, 25)
        context_before = clamp_int(before, 0, 0, 5)
        context_after = clamp_int(after, 0, 0, 5)
        flags = 0 if parse_bool(case_sensitive, False) else re.IGNORECASE

        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return self._error_payload(
                "search_logs",
                "invalid_pattern",
                f"regex compile failed: {exc}",
            )

        matches = []
        total_matches = 0
        for index, line in enumerate(self.lines):
            if not regex.search(line):
                continue

            total_matches += 1
            if len(matches) >= match_limit:
                continue

            context_start = max(0, index - context_before)
            context_end = min(len(self.lines), index + context_after + 1)
            context = []
            for ctx_index in range(context_start, context_end):
                context.append(
                    {
                        "line_number": ctx_index + 1,
                        "section": self.scope_by_line[ctx_index],
                        "text": self._clip(self.lines[ctx_index]),
                    }
                )

            matches.append(
                {
                    "line_number": index + 1,
                    "section": self.scope_by_line[index],
                    "text": self._clip(line),
                    "context": context,
                }
            )

        return self._finalize_payload(
            {
                "ok": True,
                "tool": "search_logs",
                "pattern": pattern,
                "total_matches": total_matches,
                "truncated": total_matches > match_limit,
                "matches": matches,
            }
        )

    def find_failure_markers(self, limit=10):
        match_limit = clamp_int(limit, 10, 1, 25)
        matches = []
        counts = Counter()
        total_matches = 0
        ranked_matches = []

        for index, line in enumerate(self.lines):
            marker = self._classify_marker(line)
            if not marker:
                continue

            counts[marker] += 1
            total_matches += 1
            item = {
                "line_number": index + 1,
                "section": self.scope_by_line[index],
                "marker_type": marker,
                "text": self._clip(line),
            }
            ranked_matches.append(
                (
                    self._priority_score(line, marker, index),
                    index,
                    item,
                )
            )
            if len(matches) < match_limit:
                matches.append(item)

        top_matches = [
            item
            for _, _, item in sorted(
                ranked_matches,
                key=lambda entry: (-entry[0], entry[1]),
            )[:match_limit]
        ]

        return self._finalize_payload(
            {
                "ok": True,
                "tool": "find_failure_markers",
                "total_matches": total_matches,
                "truncated": total_matches > match_limit,
                "counts_by_type": dict(counts),
                "matches": matches,
                "top_matches": top_matches,
            }
        )

    def top_error_signatures(self, top_k=5, min_occurrences=1):
        result_limit = clamp_int(top_k, 5, 1, 10)
        minimum = clamp_int(min_occurrences, 1, 1, 10)
        grouped = defaultdict(list)

        for index, line in enumerate(self.lines):
            if not self._is_error_like(line):
                continue
            signature = self._normalize_signature(line)
            if not signature:
                signature = self._clip(line).lower()
            grouped[signature].append(index)

        ranked = sorted(
            grouped.items(),
            key=lambda item: (
                -max(
                    self._priority_score(
                        self.lines[index],
                        self._classify_marker(self.lines[index]),
                        index,
                    )
                    for index in item[1]
                ),
                -len(item[1]),
                item[1][0],
            ),
        )

        signatures = []
        for signature, indexes in ranked:
            if len(indexes) < minimum:
                continue
            samples = []
            for sample_index in indexes[:3]:
                samples.append(
                    {
                        "line_number": sample_index + 1,
                        "section": self.scope_by_line[sample_index],
                        "text": self._clip(self.lines[sample_index]),
                    }
                )
            signatures.append(
                {
                    "signature": signature,
                    "count": len(indexes),
                    "samples": samples,
                }
            )
            if len(signatures) >= result_limit:
                break

        return self._finalize_payload(
            {
                "ok": True,
                "tool": "top_error_signatures",
                "signatures": signatures,
            }
        )

    def likely_root_cause(self, top_k=3):
        result_limit = clamp_int(top_k, 3, 1, 5)
        candidates = {}
        total_lines = max(1, len(self.lines))

        for index, line in enumerate(self.lines):
            if not self._is_error_like(line):
                continue

            signature = self._normalize_signature(line)
            marker = self._classify_marker(line)
            score = self._priority_score(line, marker, index)
            rationale = []

            if marker in {"azure_error", "azure_task_issue"}:
                score += 2
                rationale.append("explicit Azure error marker")
            elif marker == "exit_code":
                score += 2
                rationale.append("non-zero exit code signal")
            elif marker == "task_failed":
                score += 2
                rationale.append("task failure marker")
            elif marker in {"generic_error", "exception", "npm_error", "dotnet_error", "test_failure"}:
                score += 1
                rationale.append(f"failure marker: {marker}")

            lower = line.lower()
            if "exception" in lower or "traceback" in lower:
                score += 4
                rationale.append("exception or traceback")
            if "fatal" in lower:
                score += 3
                rationale.append("fatal keyword")
            if "timeout" in lower:
                score += 3
                rationale.append("timeout keyword")
            if "failed" in lower or "failure" in lower:
                score += 2
                rationale.append("explicit failure wording")

            if index + 1 < len(self.lines) and STACKTRACE_RE.search(self.lines[index + 1]):
                score += 3
                rationale.append("stack trace follows")

            if lower.startswith("traceback"):
                score -= 3
                rationale.append("generic traceback header")
            if NESTED_TIMESTAMP_RE.match(line):
                score -= 3
                rationale.append("nested diagnostic log excerpt")
            if "collect failure diagnostics" in self.scope_by_line[index].lower():
                score -= 4
                rationale.append("post-failure diagnostics context")
            if "publish artifacts" in self.scope_by_line[index].lower():
                score -= 2
                rationale.append("post-failure artifact phase")

            score += int((index / total_lines) * 2)

            if signature not in candidates or score > candidates[signature]["score"]:
                candidates[signature] = {
                    "summary": self._clip(line),
                    "score": score,
                    "line_number": index + 1,
                    "section": self.scope_by_line[index],
                    "evidence": self._build_evidence_window(index),
                    "rationale": sorted(set(rationale)),
                }

        ranked = sorted(
            candidates.values(),
            key=lambda item: (-item["score"], item["line_number"]),
        )

        return self._finalize_payload(
            {
                "ok": True,
                "tool": "likely_root_cause",
                "candidates": ranked[:result_limit],
                "top_candidate": ranked[0] if ranked else None,
            }
        )

    def error_timeline(self, limit=12):
        event_limit = clamp_int(limit, 12, 1, 25)
        events = []
        section_rollup = defaultdict(lambda: {"error_count": 0, "warning_count": 0})

        for index, line in enumerate(self.lines):
            marker = self._classify_marker(line)
            if not marker:
                continue

            scope = self.scope_by_line[index]
            if "warning" in marker:
                section_rollup[scope]["warning_count"] += 1
                event_kind = "warning"
            else:
                section_rollup[scope]["error_count"] += 1
                event_kind = "error"

            if len(events) < event_limit:
                events.append(
                    {
                        "line_number": index + 1,
                        "section": scope,
                        "event_kind": event_kind,
                        "marker_type": marker,
                        "text": self._clip(line),
                    }
                )

        sections = []
        for section, counts in sorted(
            section_rollup.items(),
            key=lambda item: (-item[1]["error_count"], -item[1]["warning_count"], item[0]),
        ):
            sections.append(
                {
                    "section": section,
                    "error_count": counts["error_count"],
                    "warning_count": counts["warning_count"],
                }
            )

        return self._finalize_payload(
            {
                "ok": True,
                "tool": "error_timeline",
                "events": events,
                "sections": sections[:event_limit],
            }
        )

    def get_log_excerpt(self, start_line, end_line):
        if not self.lines:
            return self._finalize_payload(
                {
                    "ok": True,
                    "tool": "get_log_excerpt",
                    "start_line": 0,
                    "end_line": 0,
                    "lines": [],
                }
            )

        start = clamp_int(start_line, 1, 1, max(1, len(self.lines)))
        end = clamp_int(end_line, start, start, max(1, len(self.lines)))
        if end - start + 1 > MAX_EXCERPT_LINES:
            end = start + MAX_EXCERPT_LINES - 1

        excerpt = []
        for index in range(start - 1, end):
            excerpt.append(
                {
                    "line_number": index + 1,
                    "section": self.scope_by_line[index],
                    "text": self._clip(self.lines[index]),
                }
            )

        return self._finalize_payload(
            {
                "ok": True,
                "tool": "get_log_excerpt",
                "start_line": start,
                "end_line": end,
                "lines": excerpt,
            }
        )

    def _build_scope_index(self):
        scopes = []
        current_file = self.log_file.name
        current_section = ""

        for line in self.lines:
            file_marker = FILE_MARKER_RE.match(line.strip())
            if file_marker:
                current_file = file_marker.group(1).strip()
                current_section = ""

            azure_section = AZURE_SECTION_RE.match(line.strip())
            if azure_section:
                current_section = self._normalize_section_name(azure_section.group(1).strip())

            if current_section:
                scopes.append(f"{current_file} :: {current_section}")
            else:
                scopes.append(current_file)

        return scopes

    def _tail_preview(self, limit):
        if not self.lines:
            return []

        start_index = max(0, len(self.lines) - limit)
        preview = []
        for index, line in enumerate(self.lines[start_index:]):
            line_number = start_index + index + 1
            preview.append(
                {
                    "line_number": line_number,
                    "section": self.scope_by_line[line_number - 1],
                    "text": self._clip(line),
                }
            )
        return preview

    def _build_evidence_window(self, index):
        start = max(0, index - 1)
        end = min(len(self.lines), index + 3)
        evidence = []
        for current in range(start, end):
            evidence.append(
                {
                    "line_number": current + 1,
                    "section": self.scope_by_line[current],
                    "text": self._clip(self.lines[current]),
                }
            )
        return evidence

    def _classify_marker(self, line):
        for name, pattern in FAILURE_PATTERNS:
            if pattern.search(line):
                return name
        return None

    def _normalize_section_name(self, section_name):
        lower = section_name.lower()
        if lower.startswith("starting:"):
            return section_name.split(":", 1)[1].strip()
        if lower.startswith("finishing:"):
            return section_name.split(":", 1)[1].strip()
        return section_name

    def _priority_score(self, line, marker, index):
        score = 1
        lower = line.lower()
        section_lower = self.scope_by_line[index].lower() if index < len(self.scope_by_line) else ""

        if marker == "azure_task_issue":
            score += 10
        elif marker == "azure_error":
            score += 9
        elif marker in {"exit_code", "task_failed"}:
            score += 7
        elif marker in {"generic_error", "exception", "npm_error", "dotnet_error", "test_failure"}:
            score += 3
        elif marker in {"azure_warning", "generic_warning"}:
            score += 1

        if "terraform apply failed" in lower:
            score += 8
        if "root failure" in lower or "pipeline blocker" in lower:
            score += 8
        if "accessdenied" in lower or "not authorized to perform" in lower:
            score += 7
        if "iam:attachrolepolicy" in lower:
            score += 7
        if "terraform" in lower and ("failed" in lower or "accessdenied" in lower):
            score += 5
        if "task terraformapply failed" in lower:
            score += 5
        if "kms:decrypt" in lower:
            score += 3
        if "panic:" in lower:
            score += 2

        if "collect failure diagnostics" in section_lower and "root failure" not in lower:
            score -= 5
        if "publish artifacts" in section_lower:
            score -= 2
        if NESTED_TIMESTAMP_RE.match(line):
            score -= 3

        return score

    def _is_error_like(self, line):
        marker = self._classify_marker(line)
        if marker and marker not in {"azure_warning", "generic_warning"}:
            return True

        lowered = line.lower()
        if " warning" in lowered or lowered.startswith("warning"):
            return False
        if re.search(r"\b(error|failed|failure|exception|fatal|timeout)\b", lowered):
            if re.search(r"\b0 failed\b", lowered):
                return False
            return True
        return False

    def _normalize_signature(self, line):
        normalized = self._sanitize(line)
        for pattern, replacement in SIGNATURE_CLEANUPS:
            normalized = pattern.sub(replacement, normalized)
        return normalized.strip().lower()[:180]

    def _sanitize(self, value):
        sanitized = value
        for pattern, replacement in REDACTION_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    def _clip(self, value):
        sanitized = self._sanitize(value)
        if len(sanitized) <= MAX_LINE_TEXT_CHARS:
            return sanitized
        return sanitized[: MAX_LINE_TEXT_CHARS - 3] + "..."

    def _finalize_payload(self, payload):
        text = str(payload)
        if len(text) <= MAX_RESULT_CHARS:
            return payload

        trimmed = dict(payload)
        trimmed["truncated"] = True
        if "matches" in trimmed:
            trimmed["matches"] = trimmed["matches"][:5]
        if "events" in trimmed:
            trimmed["events"] = trimmed["events"][:5]
        if "signatures" in trimmed:
            trimmed["signatures"] = trimmed["signatures"][:3]
        if "lines" in trimmed:
            trimmed["lines"] = trimmed["lines"][:20]
        if "tail_preview" in trimmed:
            trimmed["tail_preview"] = trimmed["tail_preview"][:3]
        return trimmed

    def _error_payload(self, tool_name, error_type, message):
        return {
            "ok": False,
            "tool": tool_name,
            "error": {
                "type": error_type,
                "message": message,
            },
        }
