import importlib.util
import sys

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

build_fallback_analysis = APP_MODULE.build_fallback_analysis
render_markdown = APP_MODULE.render_markdown
LogTools = TOOLS_MODULE.LogTools


SAMPLE_LOG = """##[section]Starting: Build
2026-03-10T10:00:01Z ##[warning] npm cache miss for package-lock
2026-03-10T10:00:02Z npm ERR! code E401 token=abcdef1234567890abcdef1234567890
##vso[task.logissue type=error]Bash exited with code '1'.
2026-03-10T10:00:03Z ##[error]Command failed with exit code 1
Traceback (most recent call last):
  File "/app/build.py", line 7, in <module>
    raise RuntimeError("deploy failed")
RuntimeError: deploy failed
##[section]Finishing: Build
"""


def write_log(tmp_path):
    log_path = tmp_path / "pipeline.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    return log_path


def test_find_failure_markers_detects_azure_markers(tmp_path):
    tools = LogTools(write_log(tmp_path))

    result = tools.find_failure_markers(limit=10)

    assert result["ok"] is True
    assert result["counts_by_type"]["azure_warning"] == 1
    assert result["counts_by_type"]["azure_task_issue"] == 1
    assert result["counts_by_type"]["azure_error"] == 1


def test_search_logs_returns_context_and_redacts_sensitive_values(tmp_path):
    tools = LogTools(write_log(tmp_path))

    result = tools.search_logs("npm ERR!", limit=5, before=1, after=1)

    assert result["ok"] is True
    assert result["total_matches"] == 1
    assert result["matches"][0]["context"]
    assert "***" in result["matches"][0]["text"]


def test_likely_root_cause_prioritizes_explicit_error_line(tmp_path):
    tools = LogTools(write_log(tmp_path))

    result = tools.likely_root_cause(top_k=2)

    assert result["ok"] is True
    assert result["top_candidate"]["line_number"] in {4, 5}
    assert "failed" in result["top_candidate"]["summary"].lower()
    assert result["top_candidate"]["evidence"]


def test_fallback_analysis_and_markdown_render(tmp_path):
    log_path = write_log(tmp_path)
    tools = LogTools(log_path)
    analysis = build_fallback_analysis(tools)
    report = {
        "status": "partial",
        "log_source": str(log_path),
        "model": "llama3:latest",
        "steps_used": 2,
        "max_steps": 6,
        "error": "mock error",
        "analysis": analysis,
        "tool_trace": [{"step": 1, "tool": "find_failure_markers", "arguments": {}, "ok": True}],
    }

    markdown = render_markdown(report)

    assert analysis["summary"]
    assert analysis["likely_root_cause"]
    assert analysis["evidence"]
    assert "## Summary" in markdown
    assert "## Tool Trace" in markdown


def test_root_cause_prefers_pipeline_blocker_over_diagnostic_excerpt(tmp_path):
    complex_log = """2026-03-10T07:13:15.043Z ##[section]Starting: Terraform apply
2026-03-10T07:13:43.014Z Error: attaching IAM Policy (arn:aws:iam::aws:policy/KMSDecryptForPayments) to IAM Role (payments-prod-task-role): api error AccessDenied: not authorized to perform iam:AttachRolePolicy
2026-03-10T07:13:44.118Z ##[error]Terraform apply failed with exit code 1
2026-03-10T07:13:44.551Z ##vso[task.logissue type=error]Terraform apply failed. Root failure is AccessDenied on iam:AttachRolePolicy for payments-prod-task-role
2026-03-10T07:13:45.633Z ##[section]Starting: Collect failure diagnostics
2026-03-10T07:13:49.441Z 2026-03-10T07:13:13Z service=refunds-api level=ERROR msg="failed to decrypt payout key" error="AccessDeniedException: kms:Decrypt is not authorized for alias/payments-runtime"
2026-03-10T07:13:49.771Z 2026-03-10T07:13:13Z service=refunds-api level=ERROR msg="startup failed" reason="cannot load encrypted runtime config"
"""
    log_path = tmp_path / "complex_pipeline.log"
    log_path.write_text(complex_log, encoding="utf-8")
    tools = LogTools(log_path)

    root_cause = tools.likely_root_cause(top_k=3)
    markers = tools.find_failure_markers(limit=5)
    signatures = tools.top_error_signatures(top_k=3, min_occurrences=1)

    assert "terraform apply failed" in root_cause["top_candidate"]["summary"].lower()
    assert "terraform apply failed" in markers["top_matches"][0]["text"].lower()
    assert "attachrolepolicy" in signatures["signatures"][0]["signature"]
