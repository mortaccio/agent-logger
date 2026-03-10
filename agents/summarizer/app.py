import argparse
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from log_tools import LogTools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("log-agent")

DEFAULT_QUESTION = (
    "Analyze why this pipeline failed. Produce a short DevOps-focused "
    "analysis with summary, top failure pattern, likely root cause, evidence, and "
    "next checks. Also add recomendations for how to fix the pipeline failure based on the log analysis. in the end add author name and date of the analysis."
)
DEFAULT_SYSTEM_PROMPT = (
    "You are an Azure DevOps pipeline log analysis agent. "
    "You do not receive the full log directly. Use the available tools to inspect "
    "only the parts you need. Start from compact tools such as get_log_overview, "
    "find_failure_markers, top_error_signatures, or likely_root_cause, and use "
    "search_logs or get_log_excerpt only when you need more evidence. "
    "Do not invent evidence. Keep evidence short and cite line numbers when "
    "possible. When you have enough information, return one valid JSON object only "
    "with these keys: summary, top_failure_pattern, likely_root_cause, confidence, "
    "evidence, next_checks. "
    "The confidence value must be one of: high, medium, low. "
    "The evidence and next_checks values must be arrays of short strings."
)
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
OLLAMA_TIMEOUT_SEC = int(os.getenv("OLLAMA_TIMEOUT_SEC", "180"))
MAX_STEPS_DEFAULT = int(os.getenv("MAX_STEPS", "6"))
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
    parser.add_argument("--log-file", required=True, help="Path to the pipeline log file.")
    parser.add_argument(
        "--question",
        default=os.getenv("AGENT_QUESTION", DEFAULT_QUESTION),
        help="Question or task for the log agent.",
    )
    parser.add_argument(
        "--output-file",
        required=True,
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
    return parser.parse_args()


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


def build_user_message(question, overview):
    compact_overview = json.dumps(overview, ensure_ascii=True, indent=2)
    return (
        f"Question:\n{question}\n\n"
        "Log metadata and initial overview:\n"
        f"{compact_overview}\n\n"
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


def build_compat_user_message(question, overview, tool_catalog):
    compact_overview = json.dumps(overview, ensure_ascii=True, indent=2)
    compact_catalog = json.dumps(tool_catalog, ensure_ascii=True, indent=2)
    return (
        f"Question:\n{question}\n\n"
        f"{COMPAT_TOOL_PROTOCOL}\n\n"
        "Available tools:\n"
        f"{compact_catalog}\n\n"
        "Log metadata and initial overview:\n"
        f"{compact_overview}\n\n"
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


def build_fallback_analysis(log_tools):
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
    log_file,
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
        "log_file": str(log_file),
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
        f"- Log file: `{report['log_file']}`",
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


def run_agent(log_tools, question, model, ollama_chat_url, max_steps):
    fallback = build_fallback_analysis(log_tools)
    tool_trace = []

    if not model:
        return "degraded", fallback, tool_trace, 0, "OLLAMA_MODEL is not set; wrote deterministic fallback analysis."
    if not ollama_chat_url:
        return "degraded", fallback, tool_trace, 0, "OLLAMA host/URL is not set; wrote deterministic fallback analysis."

    overview = log_tools.get_log_overview(limit=5)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(question, overview)},
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


def run_agent_compat(log_tools, question, model, ollama_chat_url, max_steps, fallback):
    tool_trace = []
    tool_schemas = log_tools.get_tool_schemas()
    tool_catalog = build_tool_catalog(tool_schemas)
    overview = log_tools.get_log_overview(limit=5)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_compat_user_message(question, overview, tool_catalog)},
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


def main():
    args = parse_args()
    log_path = Path(args.log_file)
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
    )

    report = build_report(
        log_file=log_path,
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


if __name__ == "__main__":
    raise SystemExit(main())
