# pr-shepherd

A headless, scheduled reviewer for a whitelist of GitHub repos. Each run uses
Claude Code or Cursor Agent to:

- **Mine** — triage unresolved feedback on your PRs, prepare local patches and
  reply drafts, and write post-merge retrospectives.
- **Others** — review PRs awaiting your review against full code in temporary
  worktrees and write self-contained HTML reports.

Reports are served by a local HTTP LaunchAgent. Notifications use `lark-cli`.

## Configuration

Copy `config.example.env` to `config.env`, and `state.example.json` to
`state.json`. Set the GitHub identity, repo whitelist, checkout paths, and
notification recipient. The real configuration and runtime files are gitignored.

Choose the CLI and model in `config.env`:

```bash
REVIEW_AGENT="cursor"
REVIEW_MODEL="grok-4.7-high"
```

`REVIEW_AGENT=claude` is the default. Claude runs with `--permission-mode auto
--permission-prompts none`; Cursor runs with `--auto-review --trust`. Both use
classifier-based approval, not unrestricted execution. An omitted `REVIEW_MODEL`
uses the CLI default; `FALLBACK_MODEL` is supported only by Claude.

## Failure behavior

The runner reads `stream-json` output and stops the agent process group on the
first reported tool error, permission rejection, command failure or API error.
A nonzero CLI exit, malformed stream, missing final result or missing completion
state also fails the run. There are no run retries. Standalone `rg` / `grep`
commands returning 1 with empty stdout/stderr mean no matches and are allowed;
exit code 2 and compound-command failures still abort. Prefer native search tools
or those standalone commands. Error text quoted inside source files is not a failure.

Reviews run sequentially in the main agent, without subagents or background
commands. Cursor's print stream does not expose nested task tool events, so
subagent delegation cannot provide immediate failure detection. Instructions
require a semantic failure such as an incomplete report to be written to
`.tmp/failure.txt`; the runner monitors this file as well as structured events.

The runner calls `lark-cli` directly on failure, with the tool/command, original
reason, run id, agent/model and log location. A failed notification is recorded in
the log, and the runner exits nonzero. An empty `NOTIFY_USER_ID` disables
notifications. Success notifications are sent by the agent before recording
`state.last_run`. A successful CLI exit alone does not count as a completed run.

## Boundaries

- Every whitelisted repo receives both pipelines. No org, identity, checkout
  path or notifier id is hardcoded in committed files.
- GitHub is read-only: no comments, thread resolution, submitted reviews or
  pushes. An environment-scoped git push URL blocks pushes.
- Local review changes are confined to `.worktrees/` and `shepherd/*` patch
  branches. Existing developer worktrees are never modified.
- A `.lock/` prevents overlapping runs. Temporary worktrees and `.tmp/` are
  cleaned on exit; reports, patch branches and logs are retained. If a runner
  is forcibly killed before cleanup, verify it has stopped before removing its
  leftover lock.

## Setup and operation

Requirements: Python 3.9+, the selected CLI (`claude` or `cursor-agent`),
`gh` (authenticated), `git`, and `lark-cli` when notifications are enabled.
Additional tools used during reviews, including Cargo, must be on `EXTRA_PATH`;
launchd starts with a minimal PATH. Authenticate the selected agent beforehand.
Use `cursor-agent --list-models` to check available model identifiers.

LaunchAgent templates live in `launchd/`. Substitute your paths and install:

```bash
sed "s#__SHEPHERD_DIR__#$PWD#g; s#__REPORT_PORT__#8899#g" \
  launchd/com.example.pr-shepherd.plist > ~/Library/LaunchAgents/com.you.pr-shepherd.plist
sed "s#__SHEPHERD_DIR__#$PWD#g; s#__REPORT_PORT__#8899#g" \
  launchd/com.example.pr-shepherd-server.plist > ~/Library/LaunchAgents/com.you.pr-shepherd-server.plist
launchctl load ~/Library/LaunchAgents/com.you.pr-shepherd.plist
launchctl load ~/Library/LaunchAgents/com.you.pr-shepherd-server.plist
```

The runner agent fires on its configured schedule; the server serves `reviews/`.
Configuration changes apply on the next invocation, without reloading launchd.
A manual `./run.sh` performs a real review and can send notifications.

Validation without network requests or real notifications:

```bash
bash -n run.sh
python3 -I test_runner.py
```

## Files

| Path | Purpose |
|---|---|
| `run.sh` | Sources local config and starts the runner |
| `runner.py` | Renders instructions, runs the selected CLI, detects failures and notifies |
| `test_runner.py` | Behavior tests using fake CLI executables |
| `INSTRUCTIONS.md` | Repo-agnostic review template |
| `templates/` | Mine triage and retrospective HTML skeletons |
| `config.example.env`, `state.example.json` | Setup examples |
| `config.env`, `state.json` | Local configuration and dedup state (gitignored) |
| `reviews/` | Generated reports (gitignored) |
| `logs/` | Agent JSON events, errors and notification failures (gitignored) |
| `.lock/`, `.tmp/`, `.worktrees/` | Runner-owned temporary state (gitignored) |
| `launchd/` | Example runner and report-server LaunchAgents |
