# Changelog

All notable changes to the Ren'Py Photoreal VN Converter are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `docs/codebase/` — codebase knowledge documentation (stack, structure, architecture, conventions, integrations, testing, concerns)
- `.github/copilot-instructions.md` — pointer to `AGENTS.md`
- `.github/agents/spec-conformance.agent.md` and `.github/agents/blast-radius.agent.md` — reviewer agents
- `.github/workflows/ci.yml` — ruff + mypy checks on pull requests and pushes
- `.github/ISSUE_TEMPLATE/` — bug report and feature request forms
- `.github/PULL_REQUEST_TEMPLATE.md` — pull request template

### Changed

- Compose: removed the exclusion-pattern workflow. Script/image exclusion prompts, syncing, and filtering are gone; compose stages and scans every live script in the game folder, so deleting a script from that folder is all that is needed. Generated `config.json` no longer contains an `exclusion` block.

### Fixed

- Agent launcher: `Ctrl+C` no longer leaves the terminal (or VS Code) unresponsive when OpenCode's TUI ignores it or dies in raw console mode. The launcher now waits interruptibly, stops the whole OpenCode process tree, restores the console input mode, and handles `Ctrl+Break` as a forced interrupt.
- Compose: native rendering now shows one progress bar for all batches, using the actual total reported by the Ren'Py runtime instead of `?`, with a spinner while the runtime enumerates states. Batch status updates in place on the bar instead of printing a new line per batch.
- Compose: native batches now render slices of one frozen state list, so the total stays constant and states cannot be skipped or duplicated across batches. The cache interns shared usage lists so it stays small and loads quickly.
- CLI progress bars: the bar now repaints only when its data changes, so external terminal output (such as mise's interrupt notice) cannot leave a duplicate bar behind. The compose bar also clears itself on close and prints one final summary on success.
