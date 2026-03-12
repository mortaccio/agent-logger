import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "agents/summarizer")

APP_SPEC = importlib.util.spec_from_file_location(
    "summarizer_app", "agents/summarizer/app.py"
)
APP_MODULE = importlib.util.module_from_spec(APP_SPEC)
APP_SPEC.loader.exec_module(APP_MODULE)

TOOLS_SPEC = importlib.util.spec_from_file_location(
    "summarizer_log_tools", "agents/summarizer/log_tools.py"
)
TOOLS_MODULE = importlib.util.module_from_spec(TOOLS_SPEC)
TOOLS_SPEC.loader.exec_module(TOOLS_MODULE)

LogTools = TOOLS_MODULE.LogTools


SAMPLE_LOG = """##[section]Starting: Build
##vso[task.logissue type=error]Bash exited with code '1'.
2026-03-10T10:00:03Z ##[error]Command failed with exit code 1
RuntimeError: deploy failed
"""


def write_log(tmp_path):
    log_path = tmp_path / "pipeline.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    return log_path


def test_run_agent_falls_back_to_compat_mode_when_model_lacks_native_tools(tmp_path, monkeypatch):
    log_tools = LogTools(write_log(tmp_path))
    calls = {"count": 0}

    def fake_call_ollama_chat(model, ollama_chat_url, messages, tools):
        calls["count"] += 1
        if calls["count"] == 1:
            assert tools
            raise APP_MODULE.OllamaRequestError(
                'Ollama HTTP error 400: {"error":"model does not support tools"}',
                status_code=400,
                detail='{"error":"model does not support tools"}',
            )
        if calls["count"] == 2:
            assert tools is None
            return {
                "content": json.dumps(
                    {
                        "action": "tool_call",
                        "tool_name": "find_failure_markers",
                        "arguments": {"limit": 2},
                    }
                )
            }
        if calls["count"] == 3:
            assert tools is None
            return {
                "content": json.dumps(
                    {
                        "action": "final",
                        "analysis": {
                            "summary": "Build failed in the task execution phase.",
                            "top_failure_pattern": "bash exited with code 1",
                            "likely_root_cause": "Command failed with exit code 1",
                            "confidence": "high",
                            "evidence": ["line 2: task.logissue error", "line 3: ##[error]Command failed"],
                            "next_checks": ["Inspect the failing script arguments."],
                        },
                    }
                )
            }
        raise AssertionError("Unexpected extra Ollama call")

    monkeypatch.setattr(APP_MODULE, "call_ollama_chat", fake_call_ollama_chat)

    status, analysis, tool_trace, steps_used, error_message = APP_MODULE.run_agent(
        log_tools=log_tools,
        question="Analyze the failure",
        model="llama3:latest",
        ollama_chat_url="http://127.0.0.1:11434/api/chat",
        max_steps=4,
    )

    assert status == "ok"
    assert error_message is None
    assert steps_used == 2
    assert len(tool_trace) == 1
    assert tool_trace[0]["tool"] == "find_failure_markers"
    assert analysis["confidence"] == "high"
    assert "exit code 1" in analysis["likely_root_cause"].lower()


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ('{"error":"registry.ollama.ai/library/llama3:latest does not support tools"}', True),
        ('{"error":"unexpected server error"}', False),
    ],
)
def test_is_unsupported_tools_error(detail, expected):
    error = APP_MODULE.OllamaRequestError("test", status_code=400, detail=detail)
    assert APP_MODULE.is_unsupported_tools_error(error) is expected


def test_derive_jenkins_api_url():
    url = "http://localhost:8080/job/petclinic%20pipeline/lastBuild/consoleText"
    assert (
        APP_MODULE.derive_jenkins_api_url(url)
        == "http://localhost:8080/job/petclinic%20pipeline/lastBuild/api/json"
    )


def test_fetch_remote_log_source_uses_jenkins_build_number(monkeypatch):
    def fake_fetch_text_url(url, timeout_sec, request_headers=None):
        if url.endswith("/consoleText"):
            return "build log text", {"etag": "abc123"}, url
        return json.dumps({"number": 51, "building": False, "result": "FAILURE"}), {}, url

    monkeypatch.setattr(APP_MODULE, "fetch_text_url", fake_fetch_text_url)

    source = APP_MODULE.fetch_remote_log_source(
        "http://localhost:8080/job/petclinic%20pipeline/lastBuild/consoleText",
        timeout_sec=5,
    )

    assert source["source_id"] == "jenkins-build:51"
    assert source["source_label"].endswith("[build #51, result=FAILURE]")
    assert source["build_info"]["number"] == 51


@pytest.mark.parametrize(
    ("raw_source", "expected_kind", "expected_value"),
    [
        ("data/input/pipeline.log", "local_file", "data/input/pipeline.log"),
        ("file:data/input/pipeline.log", "local_file", "data/input/pipeline.log"),
        ("https://example.com/consoleText", "remote_url", "https://example.com/consoleText"),
        ("url:https://example.com/consoleText", "remote_url", "https://example.com/consoleText"),
        ("-", "stdin", "-"),
        ("text:line one\nline two", "inline_text", "line one\nline two"),
    ],
)
def test_resolve_log_source_spec(raw_source, expected_kind, expected_value):
    source = APP_MODULE.resolve_log_source_spec(raw_source)

    assert source["kind"] == expected_kind
    assert source["value"] == expected_value


def test_parse_args_supports_unified_log_source_env(monkeypatch):
    monkeypatch.setenv("LOG_SOURCE", "data/input/pipeline.log")
    monkeypatch.setenv("OUTPUT_FILE", "output/analysis.md")
    monkeypatch.setattr(APP_MODULE.sys, "argv", ["app.py"])

    args = APP_MODULE.parse_args()

    assert args.log_source == "data/input/pipeline.log"
    assert args.log_source_spec["kind"] == "local_file"
    assert args.log_file == "data/input/pipeline.log"
    assert args.log_url is None


def test_read_log_from_stdin(monkeypatch):
    monkeypatch.setattr(APP_MODULE.sys, "stdin", io.StringIO("line 1\nline 2\n"))

    assert APP_MODULE.read_log_from_stdin() == "line 1\nline 2\n"


def test_build_source_headers_uses_basic_auth():
    headers = APP_MODULE.build_source_headers(
        source_username="ci-bot",
        source_password="top-secret-token",
    )

    assert headers["User-Agent"] == "log-agent/1.0"
    assert headers["Authorization"].startswith("Basic ")


def test_build_fetch_error_message_for_auth_failure():
    message = APP_MODULE.build_fetch_error_message(
        "http://localhost:8080/job/petclinic%20pipeline/lastBuild/consoleText",
        status_code=403,
        detail="Authentication required <a href='/login'>login</a>",
    )

    assert "authentication required" in message.lower()
    assert "LOG_SOURCE_USERNAME/LOG_SOURCE_PASSWORD" in message


def test_run_agent_short_circuits_successful_build(monkeypatch, tmp_path):
    log_tools = LogTools(write_log(tmp_path))

    def should_not_call_ollama(*args, **kwargs):
        raise AssertionError("Ollama should not be called for a Jenkins SUCCESS build")

    monkeypatch.setattr(APP_MODULE, "call_ollama_chat", should_not_call_ollama)

    status, analysis, tool_trace, steps_used, error_message = APP_MODULE.run_agent(
        log_tools=log_tools,
        question="Analyze the run",
        model="llama3:latest",
        ollama_chat_url="http://127.0.0.1:11434/api/chat",
        max_steps=6,
        source_context={"build_result": "SUCCESS", "build_number": 11},
    )

    assert status == "ok"
    assert error_message is None
    assert steps_used == 0
    assert tool_trace == []
    assert "success" in analysis["summary"].lower()
    assert "no pipeline failure" in analysis["likely_root_cause"].lower()


def test_state_roundtrip(tmp_path):
    state_path = tmp_path / ".log-agent-state.json"
    payload = {
        "last_processed_id": "jenkins-build:77",
        "log_url": "http://localhost:8080/job/petclinic%20pipeline/lastBuild/consoleText",
    }

    APP_MODULE.save_state(state_path, payload)

    assert APP_MODULE.load_state(state_path) == payload
    assert APP_MODULE.default_state_path(tmp_path / "analysis.md") == Path(
        tmp_path / ".log-agent-state.json"
    )
