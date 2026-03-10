# Agentic Pipeline Log Analyzer

Minimal practical MVP for Azure DevOps pipeline log analysis with Ollama.

The main runtime path is now a single containerized log agent:

- input: `pipeline.log` or any plain text log file
- execution: Python + deterministic tools + Ollama `/api/chat`
- output: `analysis.md` or `analysis.json`

## Current Repo Status

Current repo structure after the upgrade:

- `agents/summarizer/`
  - primary agentic log analyzer
  - contains CLI, Ollama integration, tool loop, Docker assets
- `examples/azure-pipelines.failed-log-agent.yml`
  - example Azure DevOps integration

## What Was Added

Main additions in `agents/summarizer/`:

- agent loop with Ollama tool calling
- automatic fallback to prompt-based tool calling for models that do not support native tools
- deterministic tools for txt logs
- controlled fallback when Ollama is unavailable
- CLI for pipeline usage
- container-friendly Dockerfile + entrypoint

Implemented tools:

- `get_log_overview`
- `search_logs`
- `find_failure_markers`
- `top_error_signatures`
- `likely_root_cause`
- `error_timeline`
- `get_log_excerpt`

All tools:

- are deterministic
- return structured JSON
- enforce bounded output
- avoid shell execution
- support `limit` / `top_k` style arguments where relevant

## Where To Edit Prompts

Recommended runtime prompt override:

- [docker-compose.yml](/home/asenic/multi-agent-digest/docker-compose.yml)
  - `services.log-agent.environment.SYSTEM_PROMPT`

Default fallback prompt in code:

- [agents/summarizer/app.py](/home/asenic/multi-agent-digest/agents/summarizer/app.py)
  - `DEFAULT_SYSTEM_PROMPT`
  - `SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", ...)`

## Ollama Config

The agent does not hardcode the active Ollama endpoint or model.

It reads them from:

- CLI: `--model`, `--ollama-host`, `--ollama-url`
- env: `OLLAMA_MODEL`, `OLLAMA_HOST`, `OLLAMA_URL`

If Ollama is unavailable, the agent writes a controlled fallback analysis instead of crashing.
If the selected model does not support native Ollama `tools`, the agent automatically switches to a JSON-based compatibility loop and keeps using the same Python tools.

## Local CLI Usage

```bash
python3 agents/summarizer/app.py \
  --log-file data/input/pipeline.log \
  --output-file output/analysis.md \
  --question "Analyze why this Azure DevOps pipeline failed and suggest the next checks." \
  --model llama3:latest \
  --ollama-host http://127.0.0.1:11434 \
  --max-steps 6
```

JSON output:

```bash
python3 agents/summarizer/app.py \
  --log-file data/input/pipeline.log \
  --output-file output/analysis.json \
  --model llama3:latest \
  --ollama-host http://127.0.0.1:11434
```

## Docker Compose

Primary compose service:

- [docker-compose.yml](/home/asenic/multi-agent-digest/docker-compose.yml)

Default compose run:

```bash
docker compose up --build
```

By default it expects:

- input log: `data/input/pipeline.log`
- output file: `output/analysis.md`

## Docker Image Details

Runtime files:

- [agents/summarizer/Dockerfile](/home/asenic/multi-agent-digest/agents/summarizer/Dockerfile)
- [agents/summarizer/entrypoint.sh](/home/asenic/multi-agent-digest/agents/summarizer/entrypoint.sh)
- [agents/summarizer/requirements.txt](/home/asenic/multi-agent-digest/agents/summarizer/requirements.txt)

Container properties:

- runs as non-root user
- compose maps container UID/GID to the host by default
- reads logs from mounted volume
- writes analysis into mounted output directory

## Azure DevOps Example

Example failed-pipeline integration:

- [examples/azure-pipelines.failed-log-agent.yml](/home/asenic/multi-agent-digest/examples/azure-pipelines.failed-log-agent.yml)

The example assumes:

- `pipeline.log` is already available in `$(Pipeline.Workspace)/pipeline.log`
- the job has Docker access
- `OLLAMA_HOST` and `OLLAMA_MODEL` are available as pipeline variables

The example:

- runs only on `failed()`
- mounts the log into the container
- writes `analysis.md`
- publishes the result as an artifact

## Output Shape

The final analysis is normalized to:

- `summary`
- `top_failure_pattern`
- `likely_root_cause`
- `confidence`
- `evidence`
- `next_checks`

Markdown output is rendered from the same structured result, so `md` and `json` stay aligned.

## Guardrails

Implemented guardrails:

- LLM sees log metadata first, not the full raw log
- tool outputs are bounded and truncated
- excerpts are line-limited
- common secrets/tokens are masked
- no tool is allowed to execute arbitrary shell commands
- tool failures return structured error payloads

## Tests

Run all tests:

```bash
pytest -q
```

Current tests cover:

- deterministic log tools
- fallback analysis shaping
