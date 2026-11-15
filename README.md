# Ren'Py Photoreal VN Converter

This project is intended to be a toolkit to convert Ren'Py based games and visual novels from 2D format to photorealistic versions using local Ollama and free open-source models.

## 🎮 Features

- **Decompile** Ren'Py `.rpyc`/`.rpymc` scripts to readable `.rpy` source
- **Extract** Ren'Py `.rpa`/`.rpi` archive files into directory structures
- **Cache** game folder relative paths for use by other converter subpackages
- **Integrated** with `mise + uv` project management
- **Extracted files** preserved alongside `.rpa` files for easier path mapping

## 📦 Quick Start

### Prerequisites

- Python 3.14+ (recommended via [mise](https://mise.jdx.dev/) or `pyenv`)
- [Ollama](https://ollama.com/) installed and running locally
- (Optional) NVIDIA API key if you want to use NVIDIA hosted models via the rate‑limit proxy
- Git (for version control)

### Installation

1. **Clone the repository** (or download the source):

   ```bash
   git clone https://github.com/your-org/renpy-photoreal-vn-converter.git
   cd renpy-photoreal-vn-converter
   ```

2. **Install dependencies** (using uv, recommended):

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

6. **Decompile and extract Ren'Py games**:

   ```bash
   python src\converter\decompile\decompile.py
   ```

   Or via mise:

   ```toml
   # .mise.toml
   [scripts]
   decompile-games = "python src\\converter\\decompile\\decompile.py"
   ```

## 📁 Project Structure

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
│       └── converter/
│           ├── __init__.py          # Entry point (main())
│           └── decompile/           # Ren'Py decompile/extract subpackage
│               ├── __init__.py      # Package init
│               ├── decompile.py     # Core logic (decompile .rpyc, extract .rpa)
│               └── README.md        # This subpackage documentation
│   └── renpy_game/              # User places Ren'Py games here
│       ├── CampMourningWood-0.25.0.1/     # Example game 1
│       │   └── game/
│       │       ├── *.rpyc                # Compiled scripts
│       │   └── CampMourningWood-0.25.0.1-pc/  # Game build directory
│       └── sample_visual_novel/           # Example game 2
│           └── game/
│               └── dialogue.rpy           # Sample decompiled file
├── uv.lock
```

## 🔧 Pipeline Overview

1. **Decompile** `.rpyc` files and extract `.rpa` archives using the `src\converter\decompile\` subpackage
2. **Create composites** from different components (backgrounds, GUI, characters, scenes)
3. **Feed composites** to a ComfyUI workflow driven by a local API‑based driver for conversion

## 📚 Recent Additions

### Decompile Subpackage (`src\converter\decompile\`)

New subpackage for handling Ren'Py script decompilation and RPA archive extraction:

- **`find_game_folders()`** - Discovers all games under `src\renpy_game\`, locates `\game` subfolders, and caches the relative path
- **`get_game_folder_rel_path()` / `set_game_folder_rel_path()`** - Reusable path caching for other subpackages
- **`decompile_rpyc_files()` / `decompile_rpyc_file()`** - Decompile `.rpyc` → `.rpy` using rpa-toolkit
- **`extract_rpa_file()`** - Extract `.rpa` archives into directory alongside the `.rpa` file, preserving relative path structure
- **`process_all_games()`** - Full workflow: find → decompile → extract

Example extraction path structure:
```
.src\renpy_game\CampMourningWood-0.25.0.1\
  └─ CampMourningWood-0.25.0.1-pc\game\
      ├─ archive.rpa          # Original file preserved
      ├─ archive/             # Extracted directory (named after .rpa stem)
      │  ├─ file1.dat
      │  └─ ...
      ├─ audio/
      ├─ chars/
      └─ *.rpy                # Decompiled .rpy files
```

### Package Management

- **rpa-toolkit** installed via: `uv add git+https://github.com/regiellis/rpa-toolkit.git@435f202e7cb9aeeaf6c006cf928d9661f8830e6e`
- Added to `pyproject.toml` dependencies
- Integrated with `mise` through `.mise.toml` scripts section

## 🛠️ Usage Patterns

### Decompile Specific Files

```python
from rpatool import decompile_rpyc_file
from pathlib import Path

result = decompile_rpyc_file(
    Path("src/renpy_game/my_game/game/script.rpyc"),
    output_path=Path("src/renpy_game/my_game/game/script.rpy")
)
```

### Extract Specific .rpa

```python
from src.converter.decompile.decompile import extract_rpa_file
extract_rpa_file(Path("src/renpy_game/my_game/archive.rpa"))
# Extracted to: src/renpy_game/my_game/archive/
```

### Using Cached Path in Other Subpackages

```python
from src.converter.decompile.decompile import find_game_folders, get_game_folder_rel_path

find_game_folders()  # Cache the path
cached = get_game_folder_rel_path()
if cached:
    game_dir = f"src/{cached}"
    # Process files in game_dir
```

## 🚧 Roadmap

- [x] **Add decompilation utilities** for `.rpyc`/`.rpa` extraction ✅
- [ ] Integrate ComfyUI local API driver
- [ ] Provide example workflow JSON for ComfyUI
- [ ] Expand Modelfiles for additional context sizes and models
- [ ] Document CI/CD and testing setup

Feel free to open issues or contribute new profiles, Modelfiles, or pipeline steps.