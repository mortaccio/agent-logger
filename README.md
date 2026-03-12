# Agentic Pipeline Log Analyzer

Minimal practical MVP for Azure DevOps pipeline log analysis with Ollama.

The main runtime path is now a single containerized log agent:

- input: one `LOG_SOURCE` value that can point to a local file, remote `consoleText` URL, stdin, or inline log text
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

Single source of truth for default prompt structure:

- [agents/summarizer/prompts.py](/home/asenic/multi-agent-digest/agents/summarizer/prompts.py)
  - `SYSTEM_PROMPT_STRUCTURE`
  - `QUESTION_TEMPLATES`

Recommended runtime overrides:

- [docker-compose.yml](/home/asenic/multi-agent-digest/docker-compose.yml)
  - `services.log-agent.environment.SYSTEM_PROMPT`
  - `services.log-agent.environment.AGENT_QUESTION`

## Ollama Config

The agent does not hardcode the active Ollama endpoint or model.

It reads them from:

- CLI: `--model`, `--ollama-host`, `--ollama-url`
- env: `OLLAMA_MODEL`, `OLLAMA_HOST`, `OLLAMA_URL`

If Ollama is unavailable, the agent writes a controlled fallback analysis instead of crashing.
If the selected model does not support native Ollama `tools`, the agent automatically switches to a JSON-based compatibility loop and keeps using the same Python tools.

## Local CLI Usage

Primary CLI input switch:

- `--log-source data/input/pipeline.log`
- `--log-source http://localhost:8080/job/petclinic%20pipeline/lastBuild/consoleText`
- `--log-source -` to read the log from stdin
- `--log-source 'text:##[error]Command failed with exit code 1'` for quick inline checks

Local file:

```bash
python3 agents/summarizer/app.py \
  --log-source data/input/pipeline.log \
  --output-file output/analysis.md \
  --question "Analyze why this Azure DevOps pipeline failed and suggest the next checks." \
  --model llama3:latest \
  --ollama-host http://127.0.0.1:11434 \
  --max-steps 6
```

Remote Jenkins `consoleText` source with polling:

```bash
export LOG_SOURCE_USERNAME=jenkins-user
export LOG_SOURCE_PASSWORD=jenkins-api-token

python3 agents/summarizer/app.py \
  --log-source http://localhost:8080/job/petclinic%20pipeline/lastBuild/consoleText \
  --output-file output/analysis.md \
  --poll-interval-seconds 30 \
  --state-file output/.log-agent-state.json \
  --model llama3:latest \
  --ollama-host http://127.0.0.1:11434
```

Stdin:

```bash
cat data/input/pipeline.log | python3 agents/summarizer/app.py \
  --log-source - \
  --output-file output/analysis.md \
  --model llama3:latest \
  --ollama-host http://127.0.0.1:11434
```

JSON output:

```bash
python3 agents/summarizer/app.py \
  --log-source data/input/pipeline.log \
  --output-file output/analysis.json \
  --model llama3:latest \
  --ollama-host http://127.0.0.1:11434
```

Backward-compatible legacy aliases still work:

- `--log-file`
- `--log-url`
- `LOG_FILE`
- `LOG_URL`

## Docker Compose

Primary compose service:

- [docker-compose.yml](/home/asenic/multi-agent-digest/docker-compose.yml)

Default compose run:

```bash
docker compose up --build
```

By default it watches:

- Jenkins log URL: `http://localhost:8080/job/petclinic%20pipeline/lastBuild/consoleText`
- output file: `output/analysis.md`
- state file: `output/.log-agent-state.json`

Fast source switching in compose:

- remote URL: set `LOG_SOURCE=http://localhost:8080/job/.../consoleText`
- project file: set `LOG_SOURCE=/data/input/pipeline.log`
- the compose file mounts `./data` into `/data` for this flow

If Jenkins protects `consoleText`, provide one of:

- `LOG_SOURCE_USERNAME` + `LOG_SOURCE_PASSWORD`
- `LOG_SOURCE_AUTH_HEADER` with a full `Authorization` header value

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
