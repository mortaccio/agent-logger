import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from log_tools import LogTools
from prompts import DEFAULT_AGENT_QUESTION, get_agent_question, get_system_prompt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("log-agent")

SYSTEM_PROMPT = get_system_prompt()
OLLAMA_TIMEOUT_SEC = int(os.getenv("OLLAMA_TIMEOUT_SEC", "180"))
MAX_STEPS_DEFAULT = int(os.getenv("MAX_STEPS", "6"))
REMOTE_SOURCE_TIMEOUT_SEC = int(os.getenv("LOG_SOURCE_TIMEOUT_SEC", "60"))
POLL_INTERVAL_DEFAULT = int(os.getenv("POLL_INTERVAL_SECONDS", "0"))
COMPAT_TOOL_PROTOCOL = (
    "Native Ollama tool calling is unavailable for this model. "
    "Use this JSON protocol instead. Return exactly one JSON object per turn and "
    "nothing else. If you need a tool, return "
    '{"action":"tool_call","tool_name":"<tool name>","arguments":{...},"reason":"..."}'
    ". Use exactly one tool per turn. If you are done, return "
    '{"action":"final","analysis":{"summary":"...","top_failure_pattern":"...",'
    '"likely_root_cause":"...","confidence":"high|medium|low","evidence":["..."],'
    '"next_checks":["..."]}}'
    ". Do not wrap the JSON in markdown."
)


class OllamaRequestError(RuntimeError):
    def __init__(self, message, status_code=None, detail=None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or message


def parse_args():
    parser = argparse.ArgumentParser(
        description="Container-friendly agentic log analyzer for logs."
    )
    parser.add_argument(
        "--log-source",
        help=(
            "Unified log source. Supports local file paths, http(s) URLs, '-' for stdin, "
            "'file:<path>', 'url:<http(s)://...>', or 'text:<raw log text>'. "
            "Falls back to LOG_SOURCE."
        ),
    )
    parser.add_argument(
        "--log-file",
        help="Legacy alias for a local log file path. Falls back to LOG_FILE.",
    )
    parser.add_argument(
        "--log-url",
        help=(
            "Legacy alias for an HTTP/HTTPS log URL, for example Jenkins consoleText. "
            "Falls back to LOG_URL."
        ),
    )
    parser.add_argument(
        "--question",
        default=get_agent_question(),
        help="Question or task for the log agent.",
    )
    parser.add_argument(
        "--output-file",
        default=os.getenv("OUTPUT_FILE"),
        help="Output file path. Use .md or .json.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OLLAMA_MODEL"),
        help="Ollama model name. Falls back to OLLAMA_MODEL.",
    )
    parser.add_argument(
        "--ollama-host",
        default=os.getenv("OLLAMA_HOST"),
        help="Ollama host, for example http://127.0.0.1:11434",
    )
    parser.add_argument(
        "--ollama-url",
        default=os.getenv("OLLAMA_URL"),
        help="Optional full Ollama URL. If set to /api/generate it will be converted to /api/chat.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=MAX_STEPS_DEFAULT,
        help="Maximum number of LLM interaction steps.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=POLL_INTERVAL_DEFAULT,
        help=(
            "When used with a remote HTTP/HTTPS log source, poll for new runs every N seconds. "
            "Use 0 for one-shot mode."
        ),
    )
    parser.add_argument(
        "--state-file",
        default=os.getenv("STATE_FILE"),
        help="Optional JSON state file used to remember the last processed remote run.",
    )
    parser.add_argument(
        "--source-timeout-sec",
        type=int,
        default=REMOTE_SOURCE_TIMEOUT_SEC,
        help="HTTP timeout for fetching remote logs and build metadata.",
    )
    parser.add_argument(
        "--source-username",
        default=os.getenv("LOG_SOURCE_USERNAME") or os.getenv("JENKINS_USERNAME"),
        help="Optional username for authenticated remote log fetch.",
    )
    parser.add_argument(
        "--source-password",
        default=(
            os.getenv("LOG_SOURCE_PASSWORD")
            or os.getenv("LOG_SOURCE_API_TOKEN")
            or os.getenv("JENKINS_API_TOKEN")
            or os.getenv("JENKINS_TOKEN")
        ),
        help="Optional password or API token for authenticated remote log fetch.",
    )
    parser.add_argument(
        "--source-auth-header",
        default=os.getenv("LOG_SOURCE_AUTH_HEADER"),
        help="Optional full Authorization header value for the remote log source.",
    )

    args = parser.parse_args()
    args.log_source = resolve_requested_log_source(args, parser)
    try:
        args.log_source_spec = resolve_log_source_spec(args.log_source)
    except ValueError as exc:
        parser.error(str(exc))

    args.log_file = None
    args.log_url = None
    if args.log_source_spec["kind"] == "local_file":
        args.log_file = args.log_source_spec["value"]
    elif args.log_source_spec["kind"] == "remote_url":
        args.log_url = args.log_source_spec["value"]
    if not args.output_file:
        parser.error("--output-file is required or set OUTPUT_FILE")
    if args.poll_interval_seconds < 0:
        parser.error("--poll-interval-seconds must be zero or a positive integer")
    if args.poll_interval_seconds and args.log_source_spec["kind"] != "remote_url":
        parser.error("--poll-interval-seconds can only be used with an HTTP/HTTPS log source")
    if args.source_timeout_sec <= 0:
        parser.error("--source-timeout-sec must be a positive integer")

    return args


def cli_option_supplied(name):
    prefix = f"{name}="
    for argument in sys.argv[1:]:
        if argument == name or argument.startswith(prefix):
            return True
    return False


def is_http_url(value):
    if not isinstance(value, str):
        return False
    parts = urlsplit(value.strip())
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def resolve_requested_log_source(args, parser):
    cli_values = []
    if cli_option_supplied("--log-source") and args.log_source:
        cli_values.append("log_source")
    if cli_option_supplied("--log-file") and args.log_file:
        cli_values.append("log_file")
    if cli_option_supplied("--log-url") and args.log_url:
        cli_values.append("log_url")
    if len(cli_values) > 1:
        parser.error("use only one of --log-source, --log-file, or --log-url")

    if "log_source" in cli_values:
        return args.log_source.strip()
    if "log_file" in cli_values:
        return f"file:{args.log_file.strip()}"
    if "log_url" in cli_values:
        return f"url:{args.log_url.strip()}"

    env_log_source = os.getenv("LOG_SOURCE")
    if isinstance(env_log_source, str) and env_log_source.strip():
        return env_log_source.strip()

    env_log_file = os.getenv("LOG_FILE")
    if isinstance(env_log_file, str) and env_log_file.strip():
        return f"file:{env_log_file.strip()}"

    env_log_url = os.getenv("LOG_URL")
    if isinstance(env_log_url, str) and env_log_url.strip():
        return f"url:{env_log_url.strip()}"

    parser.error("one of --log-source, --log-file, or --log-url is required")


def parse_file_source_value(raw_source):
    if raw_source.lower().startswith("file://"):
        parts = urlsplit(raw_source)
        path = parts.path or ""
        if parts.netloc:
            path = f"//{parts.netloc}{path}"
        return urllib.request.url2pathname(path).strip()
    return raw_source[5:].strip()


def resolve_log_source_spec(log_source):
    if not isinstance(log_source, str) or not log_source.strip():
        raise ValueError("log source must be a non-empty string")

    source_text = log_source.strip()
    lowered = source_text.lower()

    if lowered in {"-", "stdin", "stdin:"}:
        return {"kind": "stdin", "value": "-", "label": "stdin"}

    if lowered.startswith("text:"):
        return {
            "kind": "inline_text",
            "value": source_text[5:],
            "label": "inline text",
        }

    if lowered.startswith("url:"):
        url = source_text[4:].strip()
        if not is_http_url(url):
            raise ValueError("url: sources must use http:// or https://")
        return {"kind": "remote_url", "value": url, "label": url}

    if lowered.startswith("file:"):
        path = parse_file_source_value(source_text)
        if not path:
            raise ValueError("file: sources must include a path")
        return {"kind": "local_file", "value": path, "label": path}

    if is_http_url(source_text):
        return {"kind": "remote_url", "value": source_text, "label": source_text}

    return {"kind": "local_file", "value": source_text, "label": source_text}


def resolve_ollama_chat_url(ollama_host=None, ollama_url=None):
    url = None
    if ollama_url:
        url = ollama_url.strip()
    elif ollama_host:
        url = ollama_host.rstrip("/") + "/api/chat"

    if not url:
        return None

    if url.endswith("/api/generate"):
        return url[: -len("/api/generate")] + "/api/chat"
    if url.endswith("/api/chat"):
        return url
    return url.rstrip("/") + "/api/chat"


def build_source_headers(source_username=None, source_password=None, source_auth_header=None):
    headers = {"User-Agent": "log-agent/1.0"}
    if source_auth_header:
        headers["Authorization"] = source_auth_header.strip()
        return headers

    if source_username and source_password:
        token = base64.b64encode(f"{source_username}:{source_password}".encode("utf-8")).decode(
            "ascii"
        )
        headers["Authorization"] = f"Basic {token}"
    return headers


def build_fetch_error_message(url, status_code=None, detail=None, transport_error=None):
    if transport_error:
        return f"Failed to fetch {url}: {transport_error}"

    compact_detail = re.sub(r"\s+", " ", detail or "").strip()
    lowered = compact_detail.lower()
    if status_code in {401, 403} and (
        "authentication required" in lowered
        or "/login" in lowered
        or "unauthorized" in lowered
        or "forbidden" in lowered
    ):
        return (
            f"Failed to fetch {url}: authentication required. "
            "Configure LOG_SOURCE_USERNAME/LOG_SOURCE_PASSWORD or LOG_SOURCE_AUTH_HEADER."
        )

    if compact_detail:
        if len(compact_detail) > 240:
            compact_detail = compact_detail[:240] + "..."
        return f"Failed to fetch {url}: HTTP {status_code}: {compact_detail}"

    if status_code is not None:
        return f"Failed to fetch {url}: HTTP {status_code}"
    return f"Failed to fetch {url}"


def fetch_text_url(url, timeout_sec, request_headers=None):
    request = urllib.request.Request(
        url=url,
        headers=request_headers or {"User-Agent": "log-agent/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            content = response.read().decode("utf-8", errors="replace")
            headers = {key.lower(): value for key, value in response.headers.items()}
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(build_fetch_error_message(url, status_code=exc.code, detail=detail)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(build_fetch_error_message(url, transport_error=str(exc))) from exc
    except TimeoutError as exc:
        raise RuntimeError(f"Timed out while fetching {url}") from exc
    return content, headers, final_url


def derive_jenkins_api_url(log_url):
    parts = urlsplit(log_url)
    if not parts.path.endswith("/consoleText"):
        return None
    api_path = parts.path[: -len("/consoleText")] + "/api/json"
    return urlunsplit((parts.scheme, parts.netloc, api_path, parts.query, parts.fragment))


def fetch_jenkins_build_info(log_url, timeout_sec, request_headers=None):
    api_url = derive_jenkins_api_url(log_url)
    if not api_url:
        return {}

    try:
        payload_text, _, _ = fetch_text_url(api_url, timeout_sec, request_headers=request_headers)
    except RuntimeError:
        return {}

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return {}

    if not isinstance(payload, dict):
        return {}

    number = payload.get("number")
    if not isinstance(number, int):
        number = None

    return {
        "number": number,
        "building": bool(payload.get("building", False)),
        "result": payload.get("result"),
        "url": payload.get("url"),
        "full_display_name": payload.get("fullDisplayName"),
    }


def build_remote_source_id(log_text, headers, build_info):
    content_hash = hashlib.sha256(log_text.encode("utf-8")).hexdigest()
    if build_info.get("number") is not None:
        return f"jenkins-build:{build_info['number']}"
    etag = headers.get("etag")
    if etag:
        return f"etag:{etag}"
    last_modified = headers.get("last-modified")
    if last_modified:
        return f"last-modified:{last_modified}:{content_hash[:12]}"
    return f"sha256:{content_hash}"


def build_remote_source_label(log_url, build_info):
    number = build_info.get("number")
    result = build_info.get("result")
    if number is None and not result and not build_info.get("building"):
        return log_url

    parts = []
    if number is not None:
        parts.append(f"build #{number}")
    if build_info.get("building"):
        parts.append("building")
    elif isinstance(result, str) and result:
        parts.append(f"result={result}")
    return f"{log_url} [{', '.join(parts)}]"


def default_state_path(output_file):
    output_path = Path(output_file)
    return output_path.parent / ".log-agent-state.json"


def load_state(state_path):
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(state_path, payload):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def fetch_remote_log_source(log_url, timeout_sec, request_headers=None):
    content, headers, final_url = fetch_text_url(log_url, timeout_sec, request_headers=request_headers)
    build_info = fetch_jenkins_build_info(final_url, timeout_sec, request_headers=request_headers)
    return {
        "content": content,
        "source_id": build_remote_source_id(content, headers, build_info),
        "source_label": build_remote_source_label(final_url, build_info),
        "build_info": build_info,
    }


def build_source_context(log_source, build_info=None):
    build_info = build_info or {}
    build_result = build_info.get("result")
    if isinstance(build_result, str):
        build_result = build_result.strip().upper() or None
    else:
        build_result = None

    build_number = build_info.get("number")
    if not isinstance(build_number, int):
        build_number = None

    return {
        "log_source": str(log_source),
        "build_result": build_result,
        "build_number": build_number,
        "building": bool(build_info.get("building", False)),
    }


def build_user_message(question, overview, source_context=None):
    compact_overview = json.dumps(overview, ensure_ascii=True, indent=2)
    compact_source_context = json.dumps(source_context or {}, ensure_ascii=True, indent=2)
    extra_instruction = ""
    if (source_context or {}).get("build_result") == "SUCCESS":
        extra_instruction = (
            "External CI metadata says this run finished with SUCCESS. "
            "Do not report a pipeline failure unless you find direct build-level evidence "
            "that contradicts that result."
        )
    return (
        f"Question:\n{question}\n\n"
        "External source context:\n"
        f"{compact_source_context}\n\n"
        "Log metadata and initial overview:\n"
        f"{compact_overview}\n\n"
        f"{extra_instruction}\n\n"
        "Use tools when you need more detail. Return JSON only when done."
    )


def build_tool_catalog(tool_schemas):
    catalog = []
    for schema in tool_schemas:
        function = schema.get("function", {})
        catalog.append(
            {
                "name": function.get("name"),
                "description": function.get("description"),
                "parameters": function.get("parameters", {}),
            }
        )
    return catalog


def build_compat_user_message(question, overview, tool_catalog, source_context=None):
    compact_overview = json.dumps(overview, ensure_ascii=True, indent=2)
    compact_catalog = json.dumps(tool_catalog, ensure_ascii=True, indent=2)
    compact_source_context = json.dumps(source_context or {}, ensure_ascii=True, indent=2)
    extra_instruction = ""
    if (source_context or {}).get("build_result") == "SUCCESS":
        extra_instruction = (
            "External CI metadata says this run finished with SUCCESS. "
            "Do not claim a pipeline failure unless tool results contain direct contradictory evidence."
        )
    return (
        f"Question:\n{question}\n\n"
        f"{COMPAT_TOOL_PROTOCOL}\n\n"
        "Available tools:\n"
        f"{compact_catalog}\n\n"
        "External source context:\n"
        f"{compact_source_context}\n\n"
        "Log metadata and initial overview:\n"
        f"{compact_overview}\n\n"
        f"{extra_instruction}\n\n"
        "Select the next best tool or return the final analysis."
    )


def call_ollama_chat(model, ollama_chat_url, messages, tools):
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0},
    }
    if tools:
        payload["tools"] = tools
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url=ollama_chat_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SEC) as response:
            raw = response.read().decode("utf-8")
        decoded = json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise OllamaRequestError(
            f"Ollama HTTP error {exc.code}: {detail}",
            status_code=exc.code,
            detail=detail,
        ) from exc
    except urllib.error.URLError as exc:
        raise OllamaRequestError(f"Ollama connection error: {exc}") from exc
    except TimeoutError as exc:
        raise OllamaRequestError("Ollama request timed out") from exc
    except json.JSONDecodeError as exc:
        raise OllamaRequestError(f"Invalid JSON from Ollama: {exc}") from exc

    message = decoded.get("message")
    if not isinstance(message, dict):
        raise OllamaRequestError("Ollama response is missing the 'message' object")
    return message


def parse_tool_arguments(raw_arguments):
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        text = raw_arguments.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def extract_json_object(text):
    candidate = text.strip()
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_compat_action(text):
    payload = extract_json_object(text)
    if not isinstance(payload, dict):
        return None

    action = payload.get("action")
    if action == "tool_call":
        tool_name = payload.get("tool_name") or payload.get("tool")
        arguments = payload.get("arguments") or payload.get("args") or {}
        if not isinstance(tool_name, str) or not tool_name.strip():
            return None
        if not isinstance(arguments, dict):
            arguments = {}
        return {
            "action": "tool_call",
            "tool_name": tool_name.strip(),
            "arguments": arguments,
        }

    if action == "final":
        analysis = payload.get("analysis")
        if isinstance(analysis, dict):
            return {"action": "final", "analysis": analysis}
        return {"action": "final", "analysis": payload}

    analysis_keys = {
        "summary",
        "top_failure_pattern",
        "likely_root_cause",
        "confidence",
        "evidence",
        "next_checks",
    }
    if analysis_keys.intersection(payload.keys()):
        return {"action": "final", "analysis": payload}
    return None


def is_unsupported_tools_error(error):
    detail = (getattr(error, "detail", "") or str(error)).lower()
    return getattr(error, "status_code", None) == 400 and "support tools" in detail


def ensure_string(value, fallback):
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def ensure_string_list(value, fallback):
    if not isinstance(value, list):
        return fallback

    cleaned = []
    for item in value:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip())
    return cleaned or fallback


def normalize_analysis(candidate, fallback):
    if not isinstance(candidate, dict):
        return fallback

    next_checks = candidate.get("next_checks")
    if next_checks is None:
        next_checks = candidate.get("next_steps")

    normalized = {
        "summary": ensure_string(candidate.get("summary"), fallback["summary"]),
        "top_failure_pattern": ensure_string(
            candidate.get("top_failure_pattern"),
            fallback["top_failure_pattern"],
        ),
        "likely_root_cause": ensure_string(
            candidate.get("likely_root_cause"),
            fallback["likely_root_cause"],
        ),
        "confidence": ensure_string(candidate.get("confidence"), fallback["confidence"]).lower(),
        "evidence": ensure_string_list(candidate.get("evidence"), fallback["evidence"]),
        "next_checks": ensure_string_list(next_checks, fallback["next_checks"]),
    }

    if normalized["confidence"] not in {"high", "medium", "low"}:
        normalized["confidence"] = fallback["confidence"]
    return normalized


def format_evidence_items(items):
    formatted = []
    for item in items or []:
        line_number = item.get("line_number")
        section = item.get("section")
        text = item.get("text")
        if line_number and section:
            formatted.append(f"line {line_number} [{section}]: {text}")
        elif line_number:
            formatted.append(f"line {line_number}: {text}")
        elif text:
            formatted.append(text)
    return formatted


def unique_preserve_order(items):
    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def build_success_analysis(log_tools, source_context=None):
    source_context = source_context or {}
    overview = log_tools.get_log_overview(limit=5)
    markers = log_tools.find_failure_markers(limit=5)
    signatures = log_tools.top_error_signatures(top_k=1, min_occurrences=1)

    marker_total = sum(markers.get("counts_by_type", {}).values())
    line_count = overview.get("line_count", 0)
    build_number = source_context.get("build_number")
    build_label = f"build #{build_number}" if build_number is not None else "the latest build"

    top_signature = ""
    if signatures.get("signatures"):
        top_signature = signatures["signatures"][0]["signature"]

    notable_marker = None
    if markers.get("top_matches"):
        notable_marker = markers["top_matches"][0]
    elif markers.get("matches"):
        notable_marker = markers["matches"][0]

    evidence = [f"External CI metadata reports {build_label} finished with SUCCESS."]
    if notable_marker and notable_marker.get("line_number"):
        evidence.append(
            f"Error-like log content exists at line {notable_marker['line_number']}, but it did not fail the Jenkins run."
        )
    if top_signature:
        evidence.append(f"Most notable error-like signature inside the log: {top_signature}")

    next_checks = []
    if notable_marker and notable_marker.get("line_number"):
        next_checks.append(
            f"Review lines around {notable_marker['line_number']} to confirm the exception is expected runtime or test behavior."
        )
    if top_signature:
        next_checks.append(
            f"If this signature is unexpected, add assertions or exit-code propagation for: {top_signature}"
        )
    next_checks.append(
        "If this run should have failed, verify the pipeline step returns a non-zero exit code when the application/test exception occurs."
    )

    analysis = {
        "summary": (
            f"External CI metadata reports {build_label} completed with SUCCESS. "
            f"The log still contains {marker_total} error-like markers across {line_count} lines, "
            "but they did not cause a pipeline failure."
        ),
        "top_failure_pattern": "No pipeline failure detected; external build result is SUCCESS.",
        "likely_root_cause": (
            "No pipeline failure was detected. Error-like log lines appear to come from application, "
            "test, or demo runtime output captured during a successful run."
        ),
        "confidence": "high",
        "evidence": unique_preserve_order(evidence)[:5],
        "next_checks": unique_preserve_order(next_checks)[:5],
    }
    return normalize_analysis(analysis, analysis)


def build_fallback_analysis(log_tools, source_context=None):
    if (source_context or {}).get("build_result") == "SUCCESS":
        return build_success_analysis(log_tools, source_context)

    overview = log_tools.get_log_overview(limit=5)
    markers = log_tools.find_failure_markers(limit=8)
    signatures = log_tools.top_error_signatures(top_k=3, min_occurrences=1)
    root_causes = log_tools.likely_root_cause(top_k=3)

    marker_total = sum(markers.get("counts_by_type", {}).values())
    line_count = overview.get("line_count", 0)
    top_signature = ""
    if signatures.get("signatures"):
        top_signature = signatures["signatures"][0]["signature"]

    top_candidate = root_causes.get("top_candidate") or {}
    evidence = format_evidence_items(top_candidate.get("evidence"))
    if not evidence:
        evidence = format_evidence_items(markers.get("matches", [])[:3])

    if top_candidate:
        summary = (
            f"Detected {marker_total} failure markers across {line_count} log lines. "
            f"The strongest failure signal is near line {top_candidate['line_number']}."
        )
    elif marker_total:
        summary = (
            f"Detected {marker_total} failure markers across {line_count} log lines, "
            "but no single root cause candidate dominated the heuristics."
        )
    else:
        summary = (
            f"No explicit Azure failure markers were found in {line_count} log lines. "
            "The failure likely needs manual inspection of the final log section."
        )

    next_checks = []
    if top_candidate:
        next_checks.append(
            f"Inspect lines around {top_candidate['line_number']} in {top_candidate['section']}."
        )
    if top_signature:
        next_checks.append(f"Search for repeated instances of: {top_signature}")
    if markers.get("counts_by_type", {}).get("exit_code"):
        next_checks.append(
            "Check the failing task command and non-zero exit code in the Azure DevOps step output."
        )
    if markers.get("counts_by_type", {}).get("azure_task_issue"):
        next_checks.append(
            "Review the task.logissue error lines and the exact task parameters used in that stage."
        )
    if not next_checks:
        next_checks.append("Inspect the final section of the pipeline log for the first non-success marker.")
        next_checks.append("Re-run the failing job with verbose logging if the current evidence is insufficient.")

    confidence = "low"
    if top_candidate:
        confidence = "high" if top_candidate.get("score", 0) >= 11 else "medium"
    elif marker_total:
        confidence = "medium"

    analysis = {
        "summary": summary,
        "top_failure_pattern": top_signature or "No repeated error signature was extracted.",
        "likely_root_cause": top_candidate.get("summary")
        or "No single root cause candidate was isolated from the current log markers.",
        "confidence": confidence,
        "evidence": unique_preserve_order(evidence)[:5],
        "next_checks": unique_preserve_order(next_checks)[:5],
    }
    return normalize_analysis(analysis, analysis)


def build_report(
    *,
    log_source,
    question,
    output_file,
    model,
    ollama_chat_url,
    max_steps,
    status,
    analysis,
    tool_trace,
    steps_used,
    error_message=None,
):
    return {
        "status": status,
        "question": question,
        "log_source": str(log_source),
        "output_file": str(output_file),
        "model": model,
        "ollama_chat_url": ollama_chat_url,
        "max_steps": max_steps,
        "steps_used": steps_used,
        "error": error_message,
        "analysis": analysis,
        "tool_trace": tool_trace,
    }


def render_markdown(report):
    analysis = report["analysis"]
    lines = [
        "# Pipeline Failure Analysis",
        "",
        f"- Status: {report['status']}",
        f"- Log source: `{report['log_source']}`",
        f"- Model: `{report['model'] or 'not-used'}`",
        f"- Steps used: {report['steps_used']}/{report['max_steps']}",
    ]

    if report.get("error"):
        lines.extend(["", f"- Agent note: {report['error']}"])

    lines.extend(
        [
            "",
            "## Summary",
            "",
            analysis["summary"],
            "",
            "## Top Failure Pattern",
            "",
            analysis["top_failure_pattern"],
            "",
            "## Likely Root Cause",
            "",
            f"{analysis['likely_root_cause']} (confidence: {analysis['confidence']})",
            "",
            "## Evidence",
            "",
        ]
    )

    if analysis["evidence"]:
        for item in analysis["evidence"]:
            lines.append(f"- {item}")
    else:
        lines.append("- No concise evidence was extracted.")

    lines.extend(["", "## Next Checks", ""])
    for item in analysis["next_checks"]:
        lines.append(f"- {item}")

    if report["tool_trace"]:
        lines.extend(["", "## Tool Trace", ""])
        for entry in report["tool_trace"]:
            state = "ok" if entry.get("ok") else "error"
            lines.append(
                f"- step {entry['step']}: `{entry['tool']}` ({state}) with args `{json.dumps(entry['arguments'], ensure_ascii=True)}`"
            )

    return "\n".join(lines) + "\n"


def write_report(report, output_file):
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.suffix.lower() == ".json":
        output_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        return

    output_path.write_text(render_markdown(report), encoding="utf-8")


def run_agent(log_tools, question, model, ollama_chat_url, max_steps, source_context=None):
    fallback = build_fallback_analysis(log_tools, source_context=source_context)
    tool_trace = []

    if (source_context or {}).get("build_result") == "SUCCESS":
        return "ok", fallback, tool_trace, 0, None

    if not model:
        return "degraded", fallback, tool_trace, 0, "OLLAMA_MODEL is not set; wrote deterministic fallback analysis."
    if not ollama_chat_url:
        return "degraded", fallback, tool_trace, 0, "OLLAMA host/URL is not set; wrote deterministic fallback analysis."

    overview = log_tools.get_log_overview(limit=5)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_message(question, overview, source_context=source_context),
        },
    ]
    tool_schemas = log_tools.get_tool_schemas()
    last_error = None

    for step in range(1, max_steps + 1):
        logger.info("Agent step %d/%d", step, max_steps)
        try:
            message = call_ollama_chat(model, ollama_chat_url, messages, tool_schemas)
        except OllamaRequestError as exc:
            if step == 1 and is_unsupported_tools_error(exc):
                logger.warning(
                    "Model does not support native tools; switching to compatibility tool loop."
                )
                return run_agent_compat(
                    log_tools=log_tools,
                    question=question,
                    model=model,
                    ollama_chat_url=ollama_chat_url,
                    max_steps=max_steps,
                    fallback=fallback,
                    source_context=source_context,
                )
            last_error = str(exc)
            logger.error(last_error)
            return "degraded", fallback, tool_trace, step - 1, last_error

        assistant_message = {
            "role": "assistant",
            "content": message.get("content", "") or "",
        }
        if message.get("tool_calls"):
            assistant_message["tool_calls"] = message["tool_calls"]
        messages.append(assistant_message)

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            for tool_call in tool_calls:
                function_call = tool_call.get("function", {})
                tool_name = function_call.get("name", "")
                arguments = parse_tool_arguments(function_call.get("arguments"))
                logger.info("Executing tool '%s' with args %s", tool_name, arguments)
                result = log_tools.execute(tool_name, arguments)
                tool_trace.append(
                    {
                        "step": step,
                        "tool": tool_name,
                        "arguments": arguments,
                        "ok": result.get("ok", False),
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": tool_name,
                        "content": json.dumps(result, ensure_ascii=True),
                    }
                )
            continue

        parsed = extract_json_object(message.get("content", ""))
        if parsed is not None:
            normalized = normalize_analysis(parsed, fallback)
            return "ok", normalized, tool_trace, step, None

        if step < max_steps:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Return the final answer now as one valid JSON object with "
                        "keys: summary, top_failure_pattern, likely_root_cause, "
                        "confidence, evidence, next_checks. Do not use markdown."
                    ),
                }
            )

    last_error = (
        "The LLM did not return a valid final JSON response within the configured max_steps; "
        "wrote a partial deterministic analysis."
    )
    logger.warning(last_error)
    return "partial", fallback, tool_trace, max_steps, last_error


def run_agent_compat(
    log_tools,
    question,
    model,
    ollama_chat_url,
    max_steps,
    fallback,
    source_context=None,
):
    tool_trace = []
    tool_schemas = log_tools.get_tool_schemas()
    tool_catalog = build_tool_catalog(tool_schemas)
    overview = log_tools.get_log_overview(limit=5)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_compat_user_message(
                question,
                overview,
                tool_catalog,
                source_context=source_context,
            ),
        },
    ]

    for step in range(1, max_steps + 1):
        logger.info("Compatibility agent step %d/%d", step, max_steps)
        try:
            message = call_ollama_chat(model, ollama_chat_url, messages, tools=None)
        except OllamaRequestError as exc:
            error_message = str(exc)
            logger.error(error_message)
            return "degraded", fallback, tool_trace, step - 1, error_message

        content = message.get("content", "") or ""
        messages.append({"role": "assistant", "content": content})
        action = parse_compat_action(content)

        if not action:
            if step < max_steps:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was invalid. Return exactly one JSON object. "
                            "Use either action=tool_call or action=final."
                        ),
                    }
                )
                continue
            break

        if action["action"] == "tool_call":
            tool_name = action["tool_name"]
            arguments = action["arguments"]
            logger.info("Executing compat tool '%s' with args %s", tool_name, arguments)
            result = log_tools.execute(tool_name, arguments)
            tool_trace.append(
                {
                    "step": step,
                    "tool": tool_name,
                    "arguments": arguments,
                    "ok": result.get("ok", False),
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Tool result for {tool_name}:\n"
                        f"{json.dumps(result, ensure_ascii=True)}\n\n"
                        "If you need more information, return another tool_call JSON object. "
                        "If you are done, return action=final with the analysis JSON."
                    ),
                }
            )
            continue

        normalized = normalize_analysis(action["analysis"], fallback)
        return "ok", normalized, tool_trace, step, None

    error_message = (
        "The LLM did not return a valid final JSON response within the configured max_steps; "
        "wrote a partial deterministic analysis."
    )
    logger.warning(error_message)
    return "partial", fallback, tool_trace, max_steps, error_message


def analyze_log_path(log_path, log_source, args, source_context=None):
    output_path = Path(args.output_file)

    if not log_path.exists():
        logger.error("Log file not found: %s", log_path)
        return 1
    if not log_path.is_file():
        logger.error("Log path is not a file: %s", log_path)
        return 1

    max_steps = max(1, min(args.max_steps, 12))
    ollama_chat_url = resolve_ollama_chat_url(args.ollama_host, args.ollama_url)
    log_tools = LogTools(log_path)

    status, analysis, tool_trace, steps_used, error_message = run_agent(
        log_tools=log_tools,
        question=args.question,
        model=args.model,
        ollama_chat_url=ollama_chat_url,
        max_steps=max_steps,
        source_context=source_context,
    )

    report = build_report(
        log_source=log_source,
        question=args.question,
        output_file=output_path,
        model=args.model,
        ollama_chat_url=ollama_chat_url,
        max_steps=max_steps,
        status=status,
        analysis=analysis,
        tool_trace=tool_trace,
        steps_used=steps_used,
        error_message=error_message,
    )
    try:
        write_report(report, output_path)
    except PermissionError:
        logger.error(
            "Cannot write analysis to %s. Check ownership/permissions of the mounted output directory.",
            output_path,
        )
        return 1
    logger.info("Analysis written to %s", output_path)
    return 0


def analyze_log_text(log_text, log_source, args, source_context=None):
    with tempfile.TemporaryDirectory(prefix="log-agent-") as temp_dir:
        temp_path = Path(temp_dir) / "pipeline.log"
        temp_path.write_text(log_text, encoding="utf-8")
        return analyze_log_path(temp_path, log_source, args, source_context=source_context)


def read_log_from_stdin():
    log_text = sys.stdin.read()
    if not log_text.strip():
        raise RuntimeError("No log content was provided on stdin.")
    return log_text


def run_remote_once(args):
    request_headers = build_source_headers(
        source_username=args.source_username,
        source_password=args.source_password,
        source_auth_header=args.source_auth_header,
    )
    source = fetch_remote_log_source(
        args.log_url,
        args.source_timeout_sec,
        request_headers=request_headers,
    )
    source_context = build_source_context(source["source_label"], source["build_info"])
    return analyze_log_text(
        source["content"],
        source["source_label"],
        args,
        source_context=source_context,
    )


def watch_remote_log_source(args):
    state_path = Path(args.state_file) if args.state_file else default_state_path(args.output_file)
    state = load_state(state_path)
    last_processed_id = state.get("last_processed_id")
    request_headers = build_source_headers(
        source_username=args.source_username,
        source_password=args.source_password,
        source_auth_header=args.source_auth_header,
    )

    logger.info(
        "Watching %s for new runs every %d seconds",
        args.log_url,
        args.poll_interval_seconds,
    )
    logger.info("State file: %s", state_path)

    while True:
        try:
            source = fetch_remote_log_source(
                args.log_url,
                args.source_timeout_sec,
                request_headers=request_headers,
            )
        except RuntimeError as exc:
            logger.error("%s", exc)
            time.sleep(args.poll_interval_seconds)
            continue

        build_info = source["build_info"]
        build_number = build_info.get("number")

        if build_info.get("building"):
            if build_number is not None:
                logger.info("Latest run #%s is still in progress; waiting for completion.", build_number)
            else:
                logger.info("Latest run is still in progress; waiting for completion.")
            time.sleep(args.poll_interval_seconds)
            continue

        if source["source_id"] == last_processed_id:
            if build_number is not None:
                logger.info("No new completed run detected. Last processed build: #%s", build_number)
            else:
                logger.info("No new completed run detected for %s", args.log_url)
            time.sleep(args.poll_interval_seconds)
            continue

        logger.info("Processing new run from %s", source["source_label"])
        source_context = build_source_context(source["source_label"], source["build_info"])
        exit_code = analyze_log_text(
            source["content"],
            source["source_label"],
            args,
            source_context=source_context,
        )
        if exit_code == 0:
            last_processed_id = source["source_id"]
            save_state(
                state_path,
                {
                    "last_processed_id": last_processed_id,
                    "log_url": args.log_url,
                    "source_label": source["source_label"],
                    "build_number": build_number,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        time.sleep(args.poll_interval_seconds)


def main():
    args = parse_args()
    source_spec = args.log_source_spec

    if source_spec["kind"] == "local_file":
        log_path = Path(source_spec["value"])
        return analyze_log_path(log_path, source_spec["label"], args)

    if source_spec["kind"] == "inline_text":
        return analyze_log_text(source_spec["value"], source_spec["label"], args)

    if source_spec["kind"] == "stdin":
        try:
            return analyze_log_text(read_log_from_stdin(), source_spec["label"], args)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 1

    if args.poll_interval_seconds > 0:
        try:
            watch_remote_log_source(args)
        except KeyboardInterrupt:
            logger.info("Stopping remote watcher.")
            return 0
        return 0

    try:
        return run_remote_once(args)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
