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

### Fixed

- Agent launcher: `Ctrl+C` no longer leaves the terminal (or VS Code) unresponsive when OpenCode's TUI ignores it or dies in raw console mode. The launcher now waits interruptibly, stops the whole OpenCode process tree, restores the console input mode, and handles `Ctrl+Break` as a forced interrupt.
