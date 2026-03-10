import importlib.util
import json
import sys

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
