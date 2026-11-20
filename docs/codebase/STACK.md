# Technology Stack

## Core Sections (Required)

### 1) Runtime Summary

| Area | Value | Evidence |
|------|-------|----------|
| Primary language | Python 3.14+ | `.python-version` (`3.14`); `pyproject.toml` (`requires-python = ">=3.14"`) |
| Runtime + version | CPython via `uv`/`mise`; Python 3.14 | `.python-version`; `mise.toml` (`python = "3.14"`) |
| Package manager | `uv` for installs/locking; `mise` for tool versions + tasks | `pyproject.toml` `[build-system]` uses `uv_build`; `mise.toml` `[tools]` + `[tasks.*]` |
| Module/build system | `uv_build` backend; two top-level modules (`agent`, `converter`) under `src/` | `pyproject.toml` `[tool.uv.build-backend] module-name = ["agent", "converter"]` |

### 2) Production Frameworks and Dependencies

List only high-impact production dependencies (frameworks, data, transport, auth).

| Dependency | Version | Role in system | Evidence |
|------------|---------|----------------|----------|
| fastapi | >=0.163.0 | Web framework for the local NVIDIA rate-limit reverse proxy | `pyproject.toml` dependencies |
| uvicorn | >=0.54.0 | ASGI server that runs the NVIDIA proxy (`uvicorn.run`) | `pyproject.toml` dependencies; `src/agent/nvidia_proxy.py` |
| httpx | >=0.28.1 | HTTP client (proxy upstream calls; agent CLI proxy health checks) | `pyproject.toml` dependencies; `src/agent/cli.py`, `nvidia_proxy.py` |
| rpa-toolkit | git commit `435f202e...` | Ren'Py `.rpa` archive extraction and `.rpyc` decompilation | `pyproject.toml` dependencies; `src/converter/decompile/README.md` |
| pillow | >=13.0.0 | CPU image compositing (PIL) and image color-mode classification | `pyproject.toml` dependencies; `src/converter/compose/compositor.py`, `scan_image_categories` |
| numpy | >=2.5.4 | Image array operations used during compositing | `pyproject.toml` dependencies |
| tiktoken | >=0.14.0 | Token counting for the OpenCode context-window monitor | `pyproject.toml` dependencies; `src/agent/monitor.py` |
| tqdm | >=4.70.1 | Progress bars for compose/apply/pipeline runs | `pyproject.toml` dependencies; `src/converter/__init__.py` |

### 3) Development Toolchain

| Tool | Purpose | Evidence |
|------|---------|----------|
| ruff | Linting/formatting (dev) | `pyproject.toml` `[dependency-groups] dev`; `CONTRIBUTING.md` required check `uv run ruff` |
| mypy | Static type checking (dev) | `pyproject.toml` `[dependency-groups] dev`; `CONTRIBUTING.md` required check `uv run mypy src/` |

### 4) Key Commands

```bash
uv sync                          # install dependencies (preferred)
uv run ruff                      # lint (zero errors required before PR)
uv run mypy src/                 # type check (zero issues required before PR)
mise run agent <profile>         # launch OpenCode with a model profile
mise run decompile               # decompile .rpyc + extract .rpa, write config.json
mise run compose                 # interactive compose
python -m converter.compose restore  # restore game folder from source, keep composites
python -m agent <profile>        # agent CLI entry point (src/agent/__main__.py)
python -m converter.compose      # compose entry point (src/converter/compose/cli.py)
```

### 5) Environment and Config

- Config sources: `pyproject.toml`, `uv.lock`, `mise.toml`, `.python-version`, `src/agent/config.json` (bundled model profiles), `src/converter/config.json` (decompile-generated GameConfig), `opencode.json` (auto-generated OpenCode project config).
- Required env vars: `NVIDIA_API_KEY` (required for NVIDIA profiles; set via `mise.local.toml` or environment — never commit), `GH_TOKEN` (for `gh` CLI auth — never commit).
- Deployment/runtime constraints: Python 3.14+; Ollama must be running locally for Ollama profiles (`ollama list`); NVIDIA proxy enforces ~36 RPM to stay under the 40 RPM quota; no Docker/containerization present.

### 6) Evidence

- `.python-version`
- `mise.toml`
- `pyproject.toml`
- `uv.lock`
- `src/agent/config.json`
- `CONTRIBUTING.md`

## Extended Sections (Optional)

None added — repository complexity does not justify optional sections.
