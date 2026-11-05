"""Run one review, stop on its first error, and notify independently of the agent."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import uuid


class RunError(Exception):
    pass


def describe(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def checked(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RunError(f"{' '.join(command)}: exit {result.returncode}\n{result.stderr or result.stdout}")
    return result.stdout


def command_for(agent, model, session, roots, prompt):
    directories = [arg for root in roots for arg in ("--add-dir", str(root))]
    if agent == "claude":
        command = ["claude", "-p", prompt, "--session-id", session,
                   "--permission-mode", "auto", "--permission-prompts", "none",
                   "--effort", "high", "--output-format", "stream-json", "--verbose",
                   "--disallowedTools", "Agent", "Workflow"]
        fallback = os.environ.get("FALLBACK_MODEL")
        if fallback:
            command += ["--fallback-model", fallback]
    elif agent == "cursor":
        command = ["cursor-agent", "-p", prompt, "--auto-review", "--trust",
                   "--output-format", "stream-json"]
    else:
        raise RunError(f"Unknown REVIEW_AGENT: {agent!r}; expected claude or cursor")
    if model:
        command += ["--model", model]
    return command + directories


def is_search(command):
    """Recognize one search without treating quoted/escaped patterns as shell syntax."""
    if not isinstance(command, str):
        return False
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
        elif char == "\\":
            # Escaped operators are arguments, not shell control characters.
            index += 1
        elif char == "`" or command.startswith("$(", index):
            return False
        elif quote == '"':
            if char == '"':
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char in ";&|()<>\n\r":
            return False
        index += 1
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return bool(words) and Path(words[0]).name in ("rg", "grep")


def empty_search(command, result):
    if not is_search(command) or not isinstance(result, dict):
        return False
    while set(result) == {"result"} and isinstance(result["result"], dict):
        result = result["result"]
    if set(result) == {"failure"}:
        result = result["failure"]
    elif result.get("case") == "failure":
        result = result.get("value")
    if not isinstance(result, dict):
        return False
    return (result.get("exitCode", result.get("exit_code")) == 1
            and not result.get("stderr") and not result.get("stdout")
            and not result.get("interrupted") and not result.get("error"))


def result_error(value):
    """Inspect structured tool results, never source text or model prose."""
    if isinstance(value, list):
        for child in value:
            error = result_error(child)
            if error:
                return error
    if not isinstance(value, dict):
        return None
    if value.get("is_error") or value.get("isError"):
        return describe(value)
    for key in ("exitCode", "exit_code"):
        if value.get(key) not in (None, 0):
            return describe(value)
    failures = {"error", "failure", "rejected", "permissionDenied", "permission_denied",
                "spawnError", "timeout", "fileNotFound", "notFile", "fileBusy",
                "toolNotFound", "serverNotFound", "sandboxUnsupported"}
    if value.get("case") in failures:
        return describe(value)
    for key, child in value.items():
        if key in failures and child is not None and child is not False:
            return describe({key: child})
        if isinstance(child, (dict, list)):
            error = result_error(child)
            if error:
                return error
    return None


class Events:
    def __init__(self):
        self.calls = {}
        self.completed = False
        self.summary = ""

    def check(self, event):
        kind = event.get("type")
        if event.get("is_error") or kind == "error":
            raise RunError(describe(event))
        if kind == "system" and event.get("subtype") in ("permission_denied", "api_retry"):
            raise RunError(describe(event))
        if kind == "result":
            if event.get("subtype") != "success" or event.get("permission_denials"):
                raise RunError(describe(event))
            self.completed = True
            self.summary = describe(event.get("result", ""))
        metadata = event.get("tool_use_result", event.get("toolUseResult"))
        normal_search = False
        content = event.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use":
                    self.calls[block["id"]] = {"tool": block.get("name"), "input": block.get("input")}
                if block.get("type") == "tool_result":
                    # Bash can report a nonzero exit as text even without is_error.
                    text = block.get("content", "")
                    call = self.calls.get(block.get("tool_use_id"), {})
                    if (call.get("tool") == "Bash" and isinstance(text, str)
                            and re.fullmatch(r"Exit code 1\s*", text)):
                        result = dict(metadata) if isinstance(metadata, dict) else {}
                        if "exit_code" not in result:
                            result.setdefault("exitCode", 1)
                        if empty_search((call.get("input") or {}).get("command"), result):
                            normal_search = True
                            continue
                    error = block.get("is_error") or (isinstance(text, str) and re.match(r"Exit code [1-9]\d*\b", text))
                    if error:
                        raise RunError(f"{describe(call) or 'tool'}\n{describe(text)}")
        if kind == "tool_call":
            call = event.get("tool_call") or {}
            # Cursor's protobuf JSON encodes a oneof as {shellToolCall: {...}}.
            # Also accept case/value objects from recorded interaction events.
            payload = call.get("tool", call)
            if payload.get("case"):
                payload = {payload["case"]: payload.get("value", {})}
            if event.get("subtype") == "started":
                self.calls[event.get("call_id")] = describe(call)
                if "taskToolCall" in payload:
                    raise RunError("Subagent delegation is disabled: nested Cursor tool events are not exposed in stream-json")
            elif event.get("subtype") == "completed":
                for tool, data in payload.items():
                    if not isinstance(data, dict):
                        continue
                    if tool == "shellToolCall" and empty_search(data.get("args", {}).get("command"), data.get("result")):
                        continue
                    error = result_error(data.get("result"))
                    if error:
                        raise RunError(f"{tool}: {describe(data.get('args', {}))}\n{error}")
        # Claude attaches exit codes and background command failures here.
        error = None if normal_search else result_error(metadata)
        if error:
            raise RunError(error)


def stop(process):
    # Shell tools can create their own process groups. Capture descendants before
    # terminating the CLI, while parentage still identifies this invocation.
    tree = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True)
    children = {process.pid}
    pairs = [tuple(map(int, row.split())) for row in tree.stdout.splitlines()]
    while True:
        found = {pid for pid, parent in pairs if parent in children} - children
        if not found:
            break
        children.update(found)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        for pid in children - {process.pid}:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    process.wait()


def supervise(command, failure_file, log):
    events = Events()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    selector = selectors.DefaultSelector()
    buffers = {}
    stderr = []
    for pipe in (process.stdout, process.stderr):
        selector.register(pipe, selectors.EVENT_READ)
        buffers[pipe] = b""

    def consume(pipe, line):
        text = line.decode("utf-8", errors="replace")
        log.write(text + "\n")
        log.flush()
        if pipe == process.stderr:
            stderr.append(text)
            if re.match(r"\s*(?:Error:|error:|\[.*:.*error.*\])", text):
                raise RunError(text)
            return
        try:
            event = json.loads(text)
        except ValueError as error:
            raise RunError(f"Invalid agent stream event: {text[:2000]}") from error
        if not isinstance(event, dict):
            raise RunError(f"Invalid agent stream event: {text[:2000]}")
        events.check(event)

    try:
        while selector.get_map():
            if failure_file.exists():
                raise RunError(failure_file.read_text() or "Agent reported an unspecified failure")
            for key, _ in selector.select(timeout=0.1):
                pipe = key.fileobj
                chunk = os.read(pipe.fileno(), 65536)
                buffers[pipe] += chunk
                while b"\n" in buffers[pipe]:
                    line, buffers[pipe] = buffers[pipe].split(b"\n", 1)
                    if line.strip():
                        consume(pipe, line)
                if not chunk:
                    if buffers[pipe].strip():
                        consume(pipe, buffers[pipe])
                    selector.unregister(pipe)
            if process.poll() is not None and not selector.get_map():
                break
        code = process.wait()
        if code:
            raise RunError(f"Agent exited with code {code}\n" + "\n".join(stderr)[-8000:])
        if failure_file.exists():
            raise RunError(failure_file.read_text() or "Agent reported an unspecified failure")
        if not events.completed:
            raise RunError("Agent exited without a successful result event")
        return events.summary
    finally:
        stop(process)
        selector.close()
        process.stdout.close()
        process.stderr.close()


def clean_worktrees(directory, repositories):
    for child in (directory / ".worktrees").iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)
    for repository in repositories:
        checked(["git", "-C", str(repository), "worktree", "prune"])


def notify(reason, log_path, run_id, agent, model):
    recipient = os.environ.get("NOTIFY_USER_ID")
    if not recipient:
        return
    text = (f"[PR-Shepherd] 本次巡检失败\nrun: {run_id}\nagent: {agent}\n"
            f"model: {model or '(default)'}\n原因: {reason[:10000]}\n日志: {log_path}")
    output = checked(["lark-cli", "im", "+messages-send", "--user-id", recipient, "--as", "bot", "--text", text])
    response = json.loads(output)
    if response.get("ok") is not True:
        raise RunError(f"lark-cli rejected notification: {output}")
    return response


def report_failure(reason, log, log_path, run_id, agent, model):
    log.write(f"FAILED: {reason}\n")
    log.flush()
    try:
        response = notify(reason, log_path, run_id, agent, model)
        log.write(f"Failure notification: {describe(response) if response else 'disabled'}\n")
    except Exception as exc:
        log.write(f"Failure notification failed: {exc}\n")


def interrupted(signum, _frame):
    raise RunError(f"Runner interrupted by signal {signum}")


def run(directory):
    os.chdir(directory)
    now = dt.datetime.now()
    run_id = f"{now:%Y%m%dT%H%M%S}-{os.getpid()}"
    logs = directory / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"{now:%Y-%m-%d}.log"
    lock = directory / ".lock"
    try:
        lock.mkdir()
    except FileExistsError:
        with log_path.open("a") as log:
            log.write(f"[{now}] previous run still active, skipping\n")
        return 0
    agent = os.environ.get("REVIEW_AGENT", "claude")
    model = os.environ.get("REVIEW_MODEL", "")
    repositories = []
    error = None
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with log_path.open("a", buffering=1) as log:
        log.write(f"=== [{now}] run start {run_id} agent={agent} model={model or '(default)'} ===\n")
        try:
            os.environ.update(RUN_ID=run_id, SHEPHERD_DIR=str(directory),
                              WORKTREE_DIR=str(directory / ".worktrees"), DIFF_GATE_INNER="1",
                              CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="0",
                              GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="remote.origin.pushurl",
                              GIT_CONFIG_VALUE_0="no-push://pr-shepherd",
                              CARGO_TARGET_DIR=os.environ.get("SHEPHERD_CARGO_TARGET", str(Path.home() / ".cache/pr-shepherd-target")))
            table = []
            for line in os.environ.get("REPO_WHITELIST", "").splitlines():
                if not line.strip():
                    continue
                repo, path = line.strip().split("=", 1)
                repository = Path(path)
                if not repository.is_dir():
                    raise RunError(f"Missing checkout for {repo}: {path}")
                repositories.append(repository)
                table.append(f"- `{repo}` — 本地 checkout: `{path}`")
            if not repositories:
                raise RunError("REPO_WHITELIST is empty")
            for name in (".worktrees", ".tmp", "reviews"):
                (directory / name).mkdir(exist_ok=True)
            clean_worktrees(directory, repositories)
            failure_file = directory / ".tmp/failure.txt"
            failure_file.unlink(missing_ok=True)
            os.environ["FAILURE_FILE"] = str(failure_file)
            os.environ["REPO_WHITELIST_TABLE"] = "\n".join(table)
            template = (directory / "INSTRUCTIONS.md").read_text()
            instructions = directory / ".INSTRUCTIONS.local.md"
            instructions.write_text(re.sub(r"\{\{(\w+)\}\}", lambda m: os.environ[m[1]], template))
            checked(["gh", "api", "rate_limit"])
            session = str(uuid.uuid4()) if agent == "claude" else run_id
            prompt = (f"执行 PR-Shepherd 巡检，读取 {instructions} 并严格执行。"
                      f"本轮标识: {session}。任何错误立即停止，将具体原因写入 {failure_file}，"
                      "不要重试或绕过错误，不派子代理，不启动后台任务。全程中文。")
            command = command_for(agent, model, session, [directory, *repositories], prompt)
            log.write(f"run/session {session}\n")
            summary = supervise(command, failure_file, log)
            state_path = directory / "state.json"
            state = json.loads(state_path.read_text())
            if state.get("last_run") != run_id:
                raise RunError(f"Agent did not complete state.last_run for {run_id}\n{summary}")
        except (Exception, KeyboardInterrupt) as exc:
            error = f"{type(exc).__name__}: {exc}"
            report_failure(error, log, log_path, run_id, agent, model)
        finally:
            try:
                if (directory / ".worktrees").exists():
                    clean_worktrees(directory, repositories)
                if (directory / ".tmp").exists():
                    shutil.rmtree(directory / ".tmp")
            except Exception as exc:
                cleanup_error = f"Cleanup failed: {exc}"
                if error is None:
                    error = cleanup_error
                    report_failure(error, log, log_path, run_id, agent, model)
                else:
                    log.write(cleanup_error + "\n")
            finally:
                lock.rmdir()
            log.write(f"=== [{dt.datetime.now()}] run end (exit {1 if error else 0}) ===\n")
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1]).resolve()))
