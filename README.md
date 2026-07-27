# pr-shepherd

A headless, scheduled reviewer for a whitelist of GitHub repos. On each tick it
drives [Claude Code](https://claude.com/claude-code) to:

- **Mine** — find unresolved feedback on *your* open PRs (inline review threads,
  PR-level comments, and review bodies) and propose minimal fixes.
- **Others** — do a line-by-line production review of PRs awaiting your review
  (review-requested + team + CODEOWNERS), pulling each PR's head into a throwaway
  `git worktree` so it reviews the full code, and emitting a self-contained HTML
  report per PR.

Reports are served over `http://127.0.0.1:<port>` by a small LaunchAgent so chat
links are clickable. Findings are pushed to a notifier CLI (optional).

## Design

- **Whitelist-driven, repo-agnostic.** The set of repos scanned is exactly the
  `REPO_WHITELIST` in `config.env` — nothing is hardcoded to a particular repo.
- **Read-only.** It never comments, reviews, pushes, or mutates any working tree;
  local git use is limited to `fetch` + detached worktrees.
- **All environment-specific values are injected from config.** `run.sh` sources
  `config.env` and renders `INSTRUCTIONS.md` (a `{{VAR}}` template) into a local
  copy fed to Claude. The committed repo contains no org, username, path, port,
  or notifier id.

## Setup

1. **Config.** Copy the example and fill it in:
   ```bash
   cp config.example.env config.env
   $EDITOR config.env
   ```
   `config.env` is gitignored. `REPO_WHITELIST` maps each repo to a local
   checkout used for full-code review (a throwaway worktree is added at the PR
   head, so your working tree and current branch are never touched).

2. **State.** Seed the runtime state:
   ```bash
   cp state.example.json state.json
   ```

3. **LaunchAgents (macOS).** Templates live in `launchd/`. Substitute your paths
   and install:
   ```bash
   sed "s#__SHEPHERD_DIR__#$PWD#g; s#__REPORT_PORT__#8899#g" \
     launchd/com.example.pr-shepherd.plist > ~/Library/LaunchAgents/com.you.pr-shepherd.plist
   sed "s#__SHEPHERD_DIR__#$PWD#g; s#__REPORT_PORT__#8899#g" \
     launchd/com.example.pr-shepherd-server.plist > ~/Library/LaunchAgents/com.you.pr-shepherd-server.plist
   launchctl load ~/Library/LaunchAgents/com.you.pr-shepherd.plist
   launchctl load ~/Library/LaunchAgents/com.you.pr-shepherd-server.plist
   ```
   The runner agent fires on a schedule (edit `StartCalendarInterval`); the
   server agent keeps the report HTTP server alive.

4. **Manual run** (for testing):
   ```bash
   ./run.sh
   ```

## Requirements

`claude`, `gh` (authenticated), `git`, `jq`, `perl` (ships with macOS), and
optionally a notifier CLI. Put anything not on the default PATH into `EXTRA_PATH`
in `config.env` — the launchd environment is minimal.

## Files

| Path | Committed? | Purpose |
|---|---|---|
| `run.sh` | yes | Runner: loads config, renders instructions, drives Claude |
| `INSTRUCTIONS.md` | yes | Review spec — a `{{VAR}}` template, no personal data |
| `config.example.env` | yes | Config template |
| `config.env` | no (gitignored) | Real org/user/paths/notifier id |
| `state.example.json` | yes | Empty state seed |
| `state.json` | no | Runtime dedup state |
| `reviews/` | no (`.gitkeep` only) | Generated HTML reports |
| `logs/`, `.lock/`, `.worktrees/` | no | Runtime artifacts |
| `launchd/` | yes | Example LaunchAgent plists |
