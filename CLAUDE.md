# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A headless, launchd-scheduled PR reviewer. On each tick, `run.sh` drives a `claude -p` (print-mode) session that scans a whitelist of GitHub repos and runs two pipelines:

- **Mine** — finds unresolved feedback on the user's own open PRs (inline review threads, PR-level comments, review bodies) and proposes minimal fixes.
- **Others** — does a deep line-by-line review of PRs awaiting the user's review, checking out each PR head into a throwaway detached `git worktree` (so it reviews full code, not just the diff), and emits a self-contained HTML report per PR into `reviews/`.

Reports are served on `http://127.0.0.1:<port>` by a second LaunchAgent so chat links are clickable. Findings go out via `lark-cli` (optional).

## Architecture

The whole system is three moving parts:

1. **`config.env`** (gitignored; template in `config.example.env`) holds every environment-specific value: GitHub user/org/team, `REPO_WHITELIST` (repo → local checkout path, one per line), base-branch filter, report port/URL, notifier id, models. Nothing in the committed repo hardcodes an org, user, path, port, or id — that separation is a core design rule.
2. **`run.sh`** sources `config.env`, builds `--add-dir` flags and a markdown whitelist table from `REPO_WHITELIST`, renders `INSTRUCTIONS.md` (a `{{VAR}}` template) into `.INSTRUCTIONS.local.md` via perl env substitution, then invokes `claude -p` with a strict `--allowedTools` list. It also handles: a single-flight lock (`.lock/` dir, stale after 3h), pruning leftover worktrees, up to 3 retry attempts (progress detected via `state.json`'s `last_check` advancing), silent-failure alerting, and log rotation.
3. **`INSTRUCTIONS.md`** is the review spec fed to the headless Claude session — it defines both pipelines, the dedup scheme, the worktree flow, the HTML report format, and the notification message templates. It's written in Chinese; the headless run operates in Chinese.

**State/dedup:** `state.json` holds `last_check` (ISO8601), `notified_comments` (seen comment/thread keys), and `reviewed` (`"repo#123": headSha`). Unresolved inline threads have no time baseline (unresolved = pending forever); PR-level comments dedup by `createdAt > last_check` + id. A new push to a reviewed PR (head sha change) triggers re-review.

## Invariants (do not break when editing)

- **Read-only against GitHub and the source repos.** No commenting, no submitting reviews, no pushes; local git use is limited to `fetch` + detached worktrees under `.worktrees/`. The user's working trees and current branches are never touched.
- **Whitelist-driven and repo-agnostic.** Every repo in `REPO_WHITELIST` gets both pipelines every tick — nothing else is scanned, and no repo is skipped.
- The committed `INSTRUCTIONS.md` must stay a placeholder template; personal/org values only ever appear in the rendered `.INSTRUCTIONS.local.md` (gitignored).

## Hard-won headless constraints (encoded in run.sh / INSTRUCTIONS.md comments)

- `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` is required: print mode's default 600s background-wait ceiling kills long review runs.
- The instructions mandate **synchronous** subagents (`run_in_background: false`, batches of 3–4) — background agents get killed when the main session ends.
- The `--allowedTools` whitelist matches by command prefix, so the instructions forbid compound Bash commands (`&&`, `;`, pipes) and shell redirection for file writes — one command per call, files via Write/Edit.
- `run.sh` detects silent failures by checking whether `last_check` advanced; exit codes alone are unreliable.

## Commands

```bash
./run.sh                 # manual full run (needs config.env; takes a long time)
tail -f logs/$(date +%Y-%m-%d).log   # watch a run in progress
cp config.example.env config.env     # first-time setup
cp state.example.json state.json     # seed state
```

LaunchAgent install (runner + report server) is documented in README.md — templates in `launchd/` with `__SHEPHERD_DIR__` / `__REPORT_PORT__` placeholders substituted via sed.

There is no build, lint, or test suite; validation is a manual `./run.sh` plus reading the day's log.

## Commit conventions

Do not add a `Co-Authored-By` trailer (or any AI attribution) to commit messages.
