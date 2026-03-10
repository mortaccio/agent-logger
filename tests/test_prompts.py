import importlib.util


PROMPTS_SPEC = importlib.util.spec_from_file_location(
    "summarizer_prompts", "agents/summarizer/prompts.py"
)
PROMPTS_MODULE = importlib.util.module_from_spec(PROMPTS_SPEC)
PROMPTS_SPEC.loader.exec_module(PROMPTS_MODULE)


def test_blank_env_overrides_do_not_replace_default_prompts(monkeypatch):
    monkeypatch.setenv("SYSTEM_PROMPT", "   ")
    monkeypatch.setenv("AGENT_QUESTION", "")

    assert PROMPTS_MODULE.get_system_prompt() == PROMPTS_MODULE.DEFAULT_SYSTEM_PROMPT
    assert PROMPTS_MODULE.get_agent_question() == PROMPTS_MODULE.DEFAULT_AGENT_QUESTION
