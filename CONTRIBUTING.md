# Contributing

Thank you for contributing to the Ren'Py Photoreal VN Converter! This guide
describes how to raise contributions and pull requests (PRs). The full rules
live here.

## Before you start

- Check existing issues and PRs to avoid duplicate work.
- If you plan a significant change, open an issue first to discuss the
  approach before writing code.
- All changes must be made on a dedicated branch, never on `main` or the
  default branch.

## Branch naming

Use [Conventional Branch](https://conventionalbranch.org) naming:

```
<type>/<description>
```

Valid types: `feature`, `fix`, `chore`, `docs`, `refactor`, `test`, `build`, `ci`.

Examples:
- `feature/add-image-category-detection`
- `fix/ruff-import-sorting`
- `docs/add-contributing-guide`
- `chore/update-dependencies`

Lowercase only, kebab-case description, no leading/trailing hyphens.

## Branch protections

Two layers prevent direct commits to `main`:

1. **Pre-commit hook** (`.githooks/pre-commit`): blocks `git commit` on `main`,
   `master`, or `develop`. Active via `git config core.hooksPath .githooks`. If
   it fires, create a feature branch and try again.

2. **GitHub branch protection**: `main` is protected on GitHub — direct pushes
   are blocked. All changes must come through a pull request with at least one
   approving review.

## Making changes

1. Create a branch from `main`:
   ```bash
   git checkout main
   git pull origin main
   git checkout -b feature/your-change-name
   ```

2. Make your changes. Keep each commit logically grouped.

3. Follow [Conventional Commits](https://www.conventionalcommits.org/):
   ```
   <type>(<scope>): <summary>

   <body>
   ```
   Valid types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`,
   `build`, `ci`, `chore`.

   Example: `feat(decompile): add image category detection`

4. Commit on your feature branch (do not push to `main`):
   ```bash
   git add <changed-files>
   git commit -m "feat(decompile): your change summary"
   ```

## Required checks before a PR

Before raising a pull request, an agent **must** run the following checks and
confirm they pass:

```bash
uv run ruff
uv run mypy src/
```

- `uv run ruff` — lints the codebase. Must pass with zero errors.
- `uv run mypy src/` — type-checks the source tree. Must pass with zero issues.

If either check fails, fix the issues before opening the PR.

## Pull request requirements

Every PR must include:

1. **Summary** — A concise, plain-language summary of what changed and why.
   Keep it short (2–5 sentences). Do not paste full diffs or logs.
2. **Changes** — A bulleted list of files changed and what each change does.
3. **Checks performed** — Confirmation that `uv run ruff` and
   `uv run mypy` were run and passed.
4. **Linked issue** — Reference the related issue (if any) using
   `Closes #NUMBER` or `Refs #NUMBER`.

### PR summary format

```markdown
## Summary

Short description of the change (2–5 sentences). Explain the problem and the
fix, not the implementation details.

## Changes

- `src/converter/decompile/decompile.py` — refactored into submodules
- `src/converter/decompile/scan.py` — added image category detection
- `CONTRIBUTING.md` — added contribution guide

## Checks

- [ ] `uv run ruff` — passed
- [ ] `uv run mypy src/` — passed
```

Do not make the summary overly long. Link to detailed discussion in the issue
instead of repeating it here.

## Review and merge

- PRs are reviewed by the maintainer. Address review comments on your branch
  and push updates; the PR will refresh automatically.
- Do not merge your own PR. The maintainer merges after approval.
- The maintainer will squash-merge into `main`; feature branches may be
  deleted after merging.

## Questions

Open an issue for questions about process, scope, or whether a change belongs
in this project.
