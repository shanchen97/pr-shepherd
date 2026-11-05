# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A headless, launchd-scheduled PR reviewer. On each tick, `run.sh` loads configuration and starts `runner.py`, which drives Claude Code or Cursor Agent in print mode across a whitelist of GitHub repos:

- **Mine** — finds unresolved feedback on the user's own open PRs (inline review threads, PR-level comments, review bodies) and proposes minimal fixes.
- **Others** — does a deep line-by-line review of PRs awaiting the user's review, checking out each PR head into a throwaway detached `git worktree` (so it reviews full code, not just the diff), and emits a self-contained HTML report per PR into `reviews/`.

Reports are served on `http://127.0.0.1:<port>` by a second LaunchAgent so chat links are clickable. Findings go out via `lark-cli` (optional).

## Architecture

The whole system is three moving parts:

1. **`config.env`** (gitignored; template in `config.example.env`) holds every environment-specific value: GitHub user/org/team, `REPO_WHITELIST` (repo → local checkout path, one per line), base-branch filter, report port/URL, notifier id, models. Nothing in the committed repo hardcodes an org, user, path, port, or id — that separation is a core design rule.
2. **`run.sh` / `runner.py`** source configuration, render `.INSTRUCTIONS.local.md`, and invoke the selected CLI with auto review permissions. The Python runner owns the lock, worktree cleanup, JSON event monitoring and failure notifications. Any reported execution error immediately terminates the agent process group and fails the run, without retries. Logs and reports remain available after cleanup.
3. **`INSTRUCTIONS.md`** defines both pipelines, dedup, worktrees, HTML and notification formats. It requires sequential execution without subagents/background commands so tool errors are visible to the runner. Semantic failures must be written to `FAILURE_FILE`. The headless run operates in Chinese.

**State/dedup:** `state.json` holds `last_check` (ISO8601), `notified_comments` (seen comment/thread keys), and `reviewed` (`"repo#123": headSha`). Unresolved inline threads have no time baseline (unresolved = pending forever); PR-level comments dedup by `createdAt > last_check` + id. A new push to a reviewed PR (head sha change) triggers re-review.

## Invariants (do not break when editing)

- **Read-only against GitHub.** No commenting, no resolving threads, no submitting reviews, no pushes (`runner.py` sets an env-scoped `remote.origin.pushurl` that makes any push fail). Local git writes are limited to worktrees under `.worktrees/` and the Mine line's `shepherd/<repo>-<n>-<head7>` patch branches. The user's working trees, current branches and other branches are never touched.
- **Whitelist-driven and repo-agnostic.** Every repo in `REPO_WHITELIST` gets both pipelines every tick — nothing else is scanned, and no repo is skipped.
- The committed `INSTRUCTIONS.md` must stay a placeholder template; personal/org values only ever appear in the rendered `.INSTRUCTIONS.local.md` (gitignored).

## Headless constraints

- Claude uses `--permission-mode auto --permission-prompts none`; Cursor uses `--auto-review`. Do not switch to unrestricted permissions.
- No subagents or background commands: Cursor stream-json omits nested tool events, making immediate error detection impossible inside delegated work.
- Do not hide execution errors with shell chaining or fallback commands. Empty queries and findings about reviewed code are not execution failures.
- CLI exit status alone is insufficient: the runner requires a successful result event and `state.last_run` matching the current run id.
- Do not run a real review for a routine test: it can send external notifications and update local review state.

## Commands

```bash
./run.sh                 # manual full run (needs config.env; takes a long time)
tail -f logs/$(date +%Y-%m-%d).log   # watch a run in progress
cp config.example.env config.env     # first-time setup
cp state.example.json state.json     # seed state
```

LaunchAgent install (runner + report server) is documented in README.md — templates in `launchd/` with `__SHEPHERD_DIR__` / `__REPORT_PORT__` placeholders substituted via sed.

Validate with `bash -n run.sh` and `python3 -I test_runner.py`. Tests use fake CLI executables and send no real notifications.

## Commit conventions

Do not add a `Co-Authored-By` trailer (or any AI attribution) to commit messages.
