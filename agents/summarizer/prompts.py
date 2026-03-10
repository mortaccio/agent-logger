import os

SYSTEM_PROMPT_STRUCTURE = {
    "role": (
        "You are a pipeline log analysis agent."
    ),
    "context": [
        "You do not receive the full log directly.",
        "Use the available tools to inspect only the log fragments you need.",
    ],
    "workflow": [
        "Start from compact tools such as get_log_overview, find_failure_markers, top_error_signatures, or likely_root_cause.",
        "Use search_logs or get_log_excerpt only when you need more evidence.",
    ],
    "guardrails": [
        "Do not invent evidence.",
        "Keep evidence short and cite line numbers when possible.",
        "If external CI metadata says the run finished with SUCCESS, do not report a pipeline failure unless there is direct contradictory build-level evidence.",
    ],
    "output_contract": [
        "When you have enough information, return one valid JSON object only.",
        "Use exactly these keys: summary, top_failure_pattern, likely_root_cause, confidence, evidence, next_checks.",
        "The confidence value must be one of: high, medium, low.",
        "The evidence and next_checks values must be arrays of short strings.",
    ],
}

QUESTION_TEMPLATES = {
    "default": (
        "Analyze the latest pipeline run log. Explain whether the run failed or "
        "succeeded, identify the top failure pattern if any, the likely root cause, "
        "concise evidence, next checks, and practical remediation steps. "
        "Add author and timestamp to the report. Also add in the end this: "
        "\"I am happy to help you with the next checks or any other questions about this pipeline run.\""
    ),
}


def _env_text(name, default):
    value = os.getenv(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def build_system_prompt():
    ordered_sections = (
        [SYSTEM_PROMPT_STRUCTURE["role"]]
        + SYSTEM_PROMPT_STRUCTURE["context"]
        + SYSTEM_PROMPT_STRUCTURE["workflow"]
        + SYSTEM_PROMPT_STRUCTURE["guardrails"]
        + SYSTEM_PROMPT_STRUCTURE["output_contract"]
    )
    return " ".join(ordered_sections)


DEFAULT_SYSTEM_PROMPT = build_system_prompt()
DEFAULT_AGENT_QUESTION = QUESTION_TEMPLATES["default"]


def get_system_prompt():
    return _env_text("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


def get_agent_question():
    return _env_text("AGENT_QUESTION", DEFAULT_AGENT_QUESTION)
