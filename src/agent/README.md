# `agents` Subpackage README

## Overview

The `agents` subpackage (`src/renpy_photoreal_vn_converter/agent/`) provides OpenCode agent orchestration for the Ren'Py photoreal visual novel converter. It bundles:

- **Model profiles** defined in `config.json` — supporting both **Ollama** local models and **NVIDIA** hosted models (via a rate-limiting proxy).
- A **rate-limiting reverse proxy** (`nvidia_proxy.py`) that sits between OpenCode and NVIDIA's API, enforcing a ~36 RPM rate limit to stay safely below the 40 RPM quota.
- A **context-window monitor** (`monitor.py`) that watches the OpenCode session, and when the conversation approaches the context limit, saves a Markdown backup of the conversation so you don't lose your place.
- A **CLI launcher** (`cli.py`) that handles profile selection, proxy management, and OpenCode launch.

The subpackage can be run directly:

```bash
python -m agent <profile>
```

Profiles are defined in `config.json` next to this module. See the **"Profiles"** section below for the complete list.

---

## Profiles

The bundled `config.json` defines the following profiles:

| Profile Key | Provider | Model Name | Context Limit |
|---|---|---|---|
| `qwen-3-vl-128k` | `ollama` | `qwen3-vl-128k` | 131 072 |
| `qwen-3-vl-64k` | `ollama` | `qwen3-vl-64k` | 65 536 |
| `qwen-3-vl-32k` | `ollama` | `qwen3-vl-32k` | 32 768 |
| `qwen-3-vl-16k` | `ollama` | `qwen3-vl-16k` | 16 384 |
| `nvidia-glm` | `nvidia` | `z-ai/glm-5.3-flash` | 1 048 576 |
| `nvidia-nemotron-3.5-super-vl` | `nvidia` | `nvidia/nemotron-3.5-super-vl-120b-a12b` | 262 144 |
| `nvidia-nemotron-3-nano-omni` | `nvidia` | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | 262 144 |
| `deepseek-v4.1-flash` | `nvidia` | `deepseek-ai/deepseek-v4.1-flash` | 1 048 576 |
| `mistral-large-4` | `nvidia` | `mistralai/mistral-large-4-1t-a52b` | 262 144 |
| `nvidia-nemotron-3.5-lightning` | `nvidia` | `nvidia/nemotron-3.5-lightning-30b-a3b` | 1 048 576 |
| `minimax-m3.1-flash` | `nvidia` | `minimaxai/minimax-m3.1-flash` | 262 144 |
| `gpt-oss-20b` | `nvidia` | `openai/gpt-oss-20b` | 131 072 |
| `gemma-4-31b-it` | `nvidia` | `google/gemma-4-31b-it` | 262 144 |

Each profile has:
- `provider` — `"ollama"` or `"nvidia"`.
- `model_name` — the upstream model identifier.
- `context_limit` — the maximum token count the profile is allowed to use.

---

## Usage

### Launching with a profile

```bash
# Interactive picker (no profile given)
mise run agent

# Using the bundled profile
mise run agent qwen-3-vl-32k

# Using the NVIDIA proxy profile
mise run agent nvidia-nemotron-3.5-lightning
```

The same commands work without mise:

```bash
python -m agent qwen-3-vl-32k
```

### Listing available profiles

```bash
mise run agent list-profiles      # or: mise run agent --list-profiles
```

### Stopping the NVIDIA proxy

```bash
mise run agent stop-proxy         # or: mise run agent --stop-proxy
```

The CLI does the following on startup:

1. Loads the requested profile from `config.json`.
2. If the profile uses `nvidia` and `NVIDIA_API_KEY` is not set, it errors out.
3. Writes the profile's model/provider settings to the **project config** (`opencode.json` or `opencode.jsonc` in the current working directory).
4. Starts the `ConversationMonitor` in a background thread, which will save Markdown backups when the context threshold is reached.
5. Launches `opencode` (either through the NVIDIA proxy or directly via Ollama).

---

## Setup

### 1. Install dependencies

```bash
# From the repo root
pip install -e src/renpy_photoreal_vn_converter   # or use your project's workflow
```

The package depends on:
- `httpx`, `fastapi`, `uvicorn`, `tiktoken` (already listed in `pyproject.toml`).

### 2. Ollama — Pull & set local models

The Ollama profiles use models hosted locally by [Ollama](https://ollama.com/). To use them:

1. **Install Ollama** following the instructions at <https://ollama.com/download>.

2. **Pull the model** you want to use. For example, to pull the 128k context version:

   ```bash
   ollama pull qwen3-vl-128k
   ```

   (The Modelfiles in `agent/Modelfiles/` define the exact build; see the **"Modelfiles"** section below.)

3. **Verify it's running**:

   ```bash
   ollama list
   ```

4. The proxy/`opencode.json` will then point at `http://127.0.0.1:11434/v1` (the default Ollama API endpoint).

### 3. NVIDIA — API key

For NVIDIA profiles, set the `NVIDIA_API_KEY` environment variable:

```bash
export NVIDIA_API_KEY=your_key_here
```

The proxy (`nvidia_proxy.py`) will use this key to authenticate against NVIDIA's hosted APIs.

### 4. Project config (`opencode.json`)

When you launch a profile, the CLI automatically writes/updates `opencode.json` (or `opencode.jsonc`) in the current working directory. This file tells OpenCode which model to use and how to reach it.

The `_write_project_config` function (in `cli.py`) generates a config like:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "ollama/qwen3-vl-128k",
  "providers": {
    "ollama": {
      "name": "Ollama",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:11434/v1",
        "apiKey": "ollama",
        "timeout": 600000
      },
      "models": {
        "qwen3-vl-128k": {
          "name": "qwen3-vl-128k",
          "capabilities": { "tools": true },
          "limit": {
            "context": 131072,
            "output": 32768
          }
        }
      }
    }
  }
}
```

For NVIDIA profiles, the `provider` becomes `nvidia` and `baseURL` points at the local proxy `http://127.0.0.1:8000/v1`.

You can manually edit `opencode.json` if needed, but it's typically best to let the CLI manage it.

### 5. (Optional) Run the NVIDIA rate‑limit proxy manually

If you want to start/stop the proxy yourself (instead of letting the CLI do it), you can run:

```bash
python -m agent --stop-proxy   # stops any running proxy
# then
python -m agent nvidia-glm     # starts proxy + launches OpenCode
```

Or directly:

```bash
python -m agent.nvidia_proxy.run
```

The proxy listens on `127.0.0.1:8000` and exposes a health endpoint at `/_nvidia_proxy/health`.

---

## Modelfiles

The `agent/Modelfiles/` directory contains **Modelfiles** that define how Ollama models are built. A Modelfile is akin to a Dockerfile for Ollama — it sets the base image and parameters (like context size).

### Example: `qwen3vl128` (128k context)

```modelfile
FROM huihui_ai/qwen3-vl-abliterated:8b

PARAMETER num_ctx 131072
```

### Available Modelfiles

| Modelfile | Base Image | `num_ctx` |
|---|---|---|
| `qwen3vl128` | `huihui_ai/qwen3-vl-abliterated:8b` | 131 072 |
| `qwen3vl16` | `huihui_ai/qwen3-vl-abliterated:8b` | 16 384 |
| `qwen3vl32` | `huihui_ai/qwen3-vl-abliterated:8b` | 32 768 |
| `qwen3vl64` | `huihui_ai/qwen3-vl-abliterated:8b` | 65 536 |

### Using a Modelfile

1. **Build the model** from the Modelfile:

   ```bash
   # From the agent directory
   ollama create qwen3-vl-128k -f Modelfiles/qwen3vl128
   ```

   This creates a new Ollama tag `qwen3-vl-128k` based on the Modelfile settings.

2. **List your built models**:

   ```bash
   ollama list
   ```

3. The profile `qwen-3-vl-128k` in `config.json` references `qwen3-vl-128k` as the `model_name`. After building, Ollama will serve it at `http://127.0.0.1:11434/v1`, and OpenCode will use it via the `ollama` provider.

You can create additional Modelfiles to adjust context sizes, system prompts, or other Ollama parameters. The `PARAMETER num_ctx` directive controls the context window size — match it to the `context_limit` in the corresponding profile entry.

---

## Opencode.json Integration

The CLI automatically syncs the selected profile into your local `opencode.json`. The key sections it writes are:

| Section | Purpose |
|---|---|
| `model` | The `provider/model` selector OpenCode uses (e.g., `ollama/qwen3-vl-128k` or `nvidia/z-ai/glm-5.3-flash`). |
| `providers.<id>` | Provider metadata: `name`, `package`, `settings` (including `baseURL` and `apiKey`), and `models` map. |
| `providers.<id>.models.<name>.limit` | Per‑model context/output token limits, derived from the profile's `context_limit`. |
| `providers.<id>.models.<name>.capabilities` | Usually `{ "tools": true }`. |

After running `python -m agent <profile>`, you can invoke OpenCode normally:

```bash
opencode
```

And OpenCode will use the model/configuration you just wrote into `opencode.json`.

If you ever need to manually adjust `opencode.json`, the structure follows the [OpenCode config schema](https://opencode.ai/config.json). The `_write_project_config` function ensures the `$schema`, `model`, `providers`, and per‑model `limit` fields are present and consistent with the profile.

---

## Full Workflow Example

### Using an Ollama model (e.g., `qwen-3-vl-32k`)

1. **Pull / build the model** (one‑time):

   ```bash
   ollama pull qwen3-vl-32k   # or build from Modelfile: ollama create qwen3-vl-32k -f Modelfiles/qwen3vl32
   ```

2. **Start OpenCode with the profile**:

   ```bash
   python -m agent qwen-3-vl-32k
   ```

3. **What happens**:

   - The CLI writes `opencode.json` with the Ollama provider config, base URL `http://127.0.0.1:11434/v1`, and the model limit.
   - The `ConversationMonitor` starts in the background, watching token usage.
   - When the context threshold (~90% of 32 768 tokens) is approached, a Markdown backup is saved to `logs/conversations/` (e.g., `qwen-3-vl-32k_backup_20261115_143000.md`).
   - `opencode` launches and uses the `qwen3-vl-32k` model via Ollama.

### Using an NVIDIA model (e.g., `nvidia-nemotron-3.5-lightning`)

1. **Set your API key**:

   ```bash
   export NVIDIA_API_KEY=your_key
   ```

2. **Start OpenCode with the profile** (the CLI will start the rate‑limit proxy automatically):

   ```bash
   python -m agent nvidia-nemotron-3.5-lightning
   ```

3. **What happens**:

   - The CLI writes `opencode.json` with the `nvidia` provider, `baseURL: http://127.0.0.1:8000/v1`, and `apiKey: local-proxy`.
   - The NVIDIA proxy starts on `127.0.0.1:8000`, rate‑limiting requests to ≤~36 RPM.
   - The monitor starts and saves backups as above.
   - `opencode` routes all calls through the proxy, which forwards them to `https://integrate.api.nvidia.com` with your API key.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `NVIDIA_API_KEY is not set` | Missing key for NVIDIA profile | Set `export NVIDIA_API_KEY=...` or use an Ollama profile. |
| Proxy fails health check | Proxy not started or crashed | Run `python -m agent stop-proxy` then re‑launch your profile. |
| Ollama model not found | Model not pulled/built | Run `ollama pull <name>` or `ollama create <tag> -f Modelfiles/<name>`. |
| Backup not saved | Context never reaches threshold | The monitor saves at `context_limit - 4096` tokens by default. Chat longer or adjust `backup_threshold` in the monitor code. |
| `opencode.json` not updated | CLI error earlier in startup | Check the error output; the profile may be unknown or missing required fields. |

---

## Summary

- **`agents`** bundles model profiles, an NVIDIA rate‑limit proxy, and a context‑monitor for OpenCode.
- **Ollama profiles** require Ollama running locally; use Modelfiles to build custom‑context models.
- **NVIDIA profiles** need an API key; the CLI starts a local proxy that enforces rate limits.
- The CLI writes `opencode.json` automatically, so after launch you can just run `opencode` normally.
- Backups are saved as Markdown when the context threshold is hit, preserving your conversation state.

Happy coding!