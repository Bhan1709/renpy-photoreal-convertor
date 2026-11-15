# Ren'Py Photoreal VN Converter

## Project Overview

This project is intended to be a toolkit to convert Ren'Py based games and visual novels from 2D format to photorealistic versions using local Ollama and free open-source models.

### Intended Pipeline

1. **Decompile** `.rpyc` files and extract `.rpa` archives.
2. **Create composites** from different components, resulting in:
   - **BG** (backgrounds)
   - **GUI** (user interface)
   - **CHAR** (characters)
   - **SCENE** (full scenes)
3. **Feed composites** to a ComfyUI workflow driven by a local API‑based driver for conversion.

---

## Getting Started

### Prerequisites

- Python 3.10+ (recommended via [mise](https://mise.jdx.dev/) or `pyenv`).
- [Ollama](https://ollama.com/) installed and running locally.
- (Optional) NVIDIA API key if you want to use NVIDIA hosted models via the rate‑limit proxy.
- Git (for version control).

### Installation

1. **Clone the repository** (or download the source):

   ```bash
   git clone https://github.com/your-org/renpy-photoreal-vn-converter.git
   cd renpy-photoreal-vn-converter
   ```

2. **Install dependencies**:

   Using [uv](https://github.com/astral-sh/uv) (recommended):

   ```bash
   uv sync
   ```

   Or with `pip`:

   ```bash
   pip install -e .
   ```

3. **Set up Ollama models** (see the `agent/README.md` for detailed Modelfile instructions):

   - Pull or build the desired Ollama models, e.g.:

     ```bash
     ollama pull qwen3-vl-32k
     # or build from a Modelfile:
     ollama create qwen3-vl-32k -f src/renpy_photoreal_vn_converter/agent/Modelfiles/qwen3vl32
     ```

   - Verify the model is available:

     ```bash
     ollama list
     ```

4. **(Optional) Set NVIDIA API key** if using NVIDIA models:

   ```bash
   export NVIDIA_API_KEY=your_key_here
   ```

5. **Run the agent** with a profile of your choice:

   ```bash
   python -m renpy_photoreal_vn_converter.agent <profile>
   ```

   Example:

   ```bash
   python -m renpy_photoreal_vn_converter.agent qwen-3-vl-32k
   ```

   This will:
   - Write the profile configuration to `opencode.json` (or `opencode.jsonc`).
   - Start the ConversationMonitor to back up conversation state.
   - Launch OpenCode through the NVIDIA proxy (if applicable) or directly via Ollama.

---

## Project Structure (high‑level)

```
.
├── README.md                # this file
├── opencode.json            # OpenCode project config (auto‑generated)
├── pyproject.toml           # package configuration
├── uv.lock                  # lock file for uv
├── .gitignore
├── .python-version
├── mise.toml                # tool version management
├── .agents/
│   └── skills/              # git‑commit, context‑map, gh‑attach skill definitions
├── src/
│   └── renpy_photoreal_vn_converter/
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── __main__.py
│       │   ├── cli.py          # profile launcher, proxy, Ollama/NVIDIA integration
│       │   ├── config.json     # bundled model profiles
│       │   ├── config.py       # profile loading/validation
│       │   ├── constants.py    # proxy constants (host, port, etc.)
│       │   ├── monitor.py      # context‑window monitor & backup
│       │   ├── Modelfiles/     # Ollama Modelfiles (qwen3vl128, qwen3vl64, …)
│       │   ├── nvidia_proxy.py # rate‑limit reverse proxy for NVIDIA APIs
│       │   └── ...
│       └── py.typed
└── uv.lock
```

---

## Next Steps / Roadmap

- Add decompilation utilities for `.rpyc`/`.rpa` extraction.
- Integrate ComfyUI local API driver.
- Provide example workflow JSON for ComfyUI.
- Expand Modelfiles for additional context sizes and models.
- Document CI/CD and testing setup.

Feel free to open issues or contribute new profiles, Modelfiles, or pipeline steps.