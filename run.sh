#!/bin/bash
# PR-Shepherd headless runner. Invoked by launchd; see launchd/ for example agents.
# All environment-specific values live in config.env (gitignored). This script
# sources it, renders INSTRUCTIONS.md (a {{VAR}} template) with those values,
# and drives Claude. Nothing here hardcodes an org, user, path, port, or id.
set -uo pipefail

# --- locate project + load config -------------------------------------------
SHEPHERD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SHEPHERD_DIR/config.env"
if [ ! -f "$CONFIG" ]; then
    echo "missing $CONFIG — copy config.example.env to config.env and fill it in" >&2
    exit 1
fi
set -a
# shellcheck source=/dev/null
. "$CONFIG"
set +a

export PATH="${EXTRA_PATH:+$EXTRA_PATH:}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# print 模式默认只等后台任务 600s 就整体终止(会连同未完成的 review 一起杀掉)。
# 一轮深度 review 远超这个时长,必须解除上限;超时兜底交给 .lock 的 3h stale 判定。
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0

LOG_DIR="$SHEPHERD_DIR/logs"
LOCK_DIR="$SHEPHERD_DIR/.lock"
WORKTREE_DIR="$SHEPHERD_DIR/.worktrees"
export SHEPHERD_DIR WORKTREE_DIR
mkdir -p "$LOG_DIR" "$SHEPHERD_DIR/reviews" "$WORKTREE_DIR"
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"

# --- build the repo whitelist: --add-dir list + a markdown table for the prompt
ADD_DIRS=(--add-dir "$SHEPHERD_DIR")
REPO_WHITELIST_TABLE=""
while IFS='=' read -r _repo _path; do
    _repo="$(echo "$_repo" | tr -d '[:space:]')"
    [ -z "$_repo" ] && continue
    REPO_WHITELIST_TABLE="${REPO_WHITELIST_TABLE}- \`${_repo}\` — 本地 checkout: \`${_path}\`
"
    [ -d "$_path" ] && ADD_DIRS+=(--add-dir "$_path")
done <<EOF
${REPO_WHITELIST:-}
EOF
export REPO_WHITELIST_TABLE

# --- render INSTRUCTIONS template with config values ------------------------
INSTRUCTIONS_TPL="$SHEPHERD_DIR/INSTRUCTIONS.md"
INSTRUCTIONS="$SHEPHERD_DIR/.INSTRUCTIONS.local.md"
perl -pe 's/\{\{(\w+)\}\}/exists $ENV{$1} ? $ENV{$1} : $&/ge' "$INSTRUCTIONS_TPL" > "$INSTRUCTIONS"

# --- single-flight lock -----------------------------------------------------
# A long review run must not be overlapped by the next tick.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +180 2>/dev/null)" ]; then
        echo "[$(date '+%F %T')] stale lock (>3h), taking over" >>"$LOG"
        rm -rf "$LOCK_DIR" && mkdir "$LOCK_DIR"
    else
        echo "[$(date '+%F %T')] previous run still active, skipping" >>"$LOG"
        exit 0
    fi
fi
trap 'rm -rf "$LOCK_DIR"' EXIT

echo "=== [$(date '+%F %T')] run start ===" >>"$LOG"

# Prune any leftover review worktrees from a killed previous run (never touches
# the source repos' working trees — these are detached worktrees under .worktrees).
rm -rf "${WORKTREE_DIR:?}"/* 2>/dev/null
while IFS='=' read -r _repo _path; do
    _repo="$(echo "$_repo" | tr -d '[:space:]')"
    [ -z "$_repo" ] && continue
    [ -d "$_path" ] && git -C "$_path" worktree prune 2>/dev/null
done <<EOF
${REPO_WHITELIST:-}
EOF

PREV_CHECK=$(jq -r '.last_check' "$SHEPHERD_DIR/state.json" 2>/dev/null)

# Repo-agnostic cwd: the scan is driven by the whitelist, not by any one repo.
cd "$SHEPHERD_DIR" || exit 1

run_once() {
    claude -p "执行 PR-Shepherd 巡检:读取 $INSTRUCTIONS 并严格按其执行(Mine=我自己 PR 的未解决评论;Others=待我 review 的 PR 深度 review + 本地 HTML 报告)。只扫描指令白名单里列出的仓库,逐个都要过。无新内容则静默更新 state 后结束。全程使用中文。" \
        "${ADD_DIRS[@]}" \
        --model "$REVIEW_MODEL" \
        --fallback-model "$FALLBACK_MODEL" \
        --effort high \
        --permission-mode acceptEdits \
        --allowedTools \
            "Bash(gh api:*)" "Bash(gh search:*)" "Bash(gh pr view:*)" "Bash(gh pr diff:*)" \
            "Bash(gh pr list:*)" "Bash(gh pr checks:*)" "Bash(gh run list:*)" "Bash(gh run view:*)" \
            "Bash(git -C:*)" "Bash(git fetch:*)" "Bash(git worktree:*)" "Bash(git show:*)" "Bash(git log:*)" \
            "Bash(lark-cli im +messages-send:*)" \
            "Bash(date:*)" "Bash(jq:*)" "Bash(base64:*)" "Bash(mkdir:*)" "Bash(ls:*)" \
            "Read" "Glob" "Grep" "Write" "Edit" "TodoWrite" "Agent" \
        --output-format text \
        >>"$LOG" 2>&1
}

# 429/过载会让整轮秒挂。重试两次,间隔递增;已完成的 PR 靠 state 去重不会重审。
RC=1
for attempt in 1 2 3; do
    echo "--- attempt $attempt [$(date '+%F %T')] ---" >>"$LOG"
    run_once
    RC=$?
    NEW_CHECK=$(jq -r '.last_check' "$SHEPHERD_DIR/state.json" 2>/dev/null)
    [ "$NEW_CHECK" != "$PREV_CHECK" ] && break
    [ $attempt -lt 3 ] && echo "[$(date '+%F %T')] attempt $attempt 未完成(exit $RC),${attempt}00s 后重试" >>"$LOG" && sleep $((attempt * 100))
done

echo "=== [$(date '+%F %T')] run end (exit $RC) ===" >>"$LOG"

# 静默失败检测:巡检结束必然推进 last_check。没推进说明这轮被中途杀掉或跑飞了,
# 而 launchd 只看退出码是看不出来的 —— 主动告警,否则用户只会以为"这小时没新东西"。
NEW_CHECK=$(jq -r '.last_check' "$SHEPHERD_DIR/state.json" 2>/dev/null)
if [ "$NEW_CHECK" = "$PREV_CHECK" ]; then
    echo "[$(date '+%F %T')] WARN: last_check 未推进($PREV_CHECK),本轮可能未完成" >>"$LOG"
    if [ -n "${NOTIFY_USER_ID:-}" ]; then
        lark-cli im +messages-send --user-id "$NOTIFY_USER_ID" --as bot \
            --text "[PR-Shepherd] 本轮巡检未正常完成(last_check 未推进,exit $RC),详见 $LOG" >>"$LOG" 2>&1
    fi
fi

find "$LOG_DIR" -name '*.log' -mtime +14 -delete 2>/dev/null
exit 0
