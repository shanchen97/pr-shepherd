"""Behavior tests with fake CLIs; no network, real reviews or notifications."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
name = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ['TEST_ROOT'])
with (root / 'calls.jsonl').open('a') as f:
    f.write(json.dumps({'name': name, 'args': sys.argv[1:]}) + '\\n')
scenario = os.environ['SCENARIO']
if name == 'gh' and scenario == 'preflight':
    print('GitHub authentication expired', file=sys.stderr)
    sys.exit(1)
if name == 'lark-cli':
    if scenario == 'notify_error':
        print('Lark token expired', file=sys.stderr)
        sys.exit(9)
    if scenario == 'notify_rejected':
        print(json.dumps({'ok': False, 'error': 'recipient unavailable'}))
        sys.exit(0)
    print(json.dumps({'ok': True}))
    sys.exit(0)
if name in ('gh', 'git'):
    sys.exit(0)
def emit(event):
    print(json.dumps(event), flush=True)
if scenario.startswith('search_'):
    pattern = os.environ.get('SEARCH_PATTERN', '[' if scenario == 'search_error' else 'symbol-that-does-not-exist')
    search = subprocess.run(['grep', pattern, str(root / 'state.json')], capture_output=True, text=True)
    shell_pattern = os.environ.get('SEARCH_SHELL_PATTERN', repr(pattern))
    command = os.environ.get('SEARCH_COMMAND', 'grep') + ' ' + shell_pattern + ' state.json'
    if scenario == 'search_compound':
        command += os.environ.get('SEARCH_SUFFIX', ' && false')
    if name == 'claude':
        emit({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'search', 'name': 'Bash', 'input': {'command': command}}]}})
        emit({'type': 'user', 'tool_use_result': {'exitCode': search.returncode, 'stdout': search.stdout, 'stderr': search.stderr}, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'search', 'is_error': True, 'content': 'Exit code ' + str(search.returncode) + '\\n' + search.stderr}]}})
    else:
        emit({'type': 'tool_call', 'subtype': 'completed', 'tool_call': {'shellToolCall': {'args': {'command': command}, 'result': {'failure': {'exitCode': search.returncode, 'stdout': search.stdout, 'stderr': search.stderr}}}}})
    if scenario == 'search_empty':
        scenario = 'success'
if scenario == 'child_error':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    (root / 'child.pid').write_text(str(child.pid))
if scenario in ('tool_error', 'notify_error', 'notify_rejected', 'child_error'):
    emit({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'python3 inspect.py'}}]}})
    emit({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'is_error': True, 'content': 'This command requires approval'}]}})
elif scenario == 'cursor_error':
    emit({'type': 'tool_call', 'subtype': 'completed', 'tool_call': {'shellToolCall': {'args': {'command': 'cargo check'}, 'result': {'failure': {'exitCode': 127, 'stderr': 'cargo: command not found'}}}}})
elif scenario == 'cursor_denied':
    emit({'type': 'tool_call', 'subtype': 'completed', 'tool_call': {'readToolCall': {'args': {'path': 'source.rs'}, 'result': {'permissionDenied': {'reason': 'read blocked'}}}}})
elif scenario == 'cursor_subagent':
    emit({'type': 'tool_call', 'subtype': 'started', 'tool_call': {'taskToolCall': {'args': {'prompt': 'review'}}}})
elif scenario == 'api_error':
    emit({'type': 'system', 'subtype': 'api_retry', 'error': 'rate limit exceeded'})
elif scenario == 'command_error':
    emit({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'Exit code 2\\ninvalid command arguments'}]}})
elif scenario == 'marker':
    pathlib.Path(os.environ['FAILURE_FILE']).write_text('Report validation failed: diff is incomplete')
elif scenario == 'exit_error':
    print('backend unavailable', file=sys.stderr)
    sys.exit(7)
elif scenario == 'missing_result':
    sys.exit(0)
elif scenario == 'missing_state':
    emit({'type': 'result', 'subtype': 'success', 'is_error': False})
    sys.exit(0)
elif scenario == 'success':
    emit({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'Source contains: Error: permission denied'}]}})
    emit({'type': 'tool_call', 'subtype': 'completed', 'tool_call': {'shellToolCall': {'args': {'command': 'check'}, 'result': {'success': {'exitCode': 0, 'stdout': 'error is just source text'}}}}})
    (root / 'state.json').write_text(json.dumps({'last_run': os.environ['RUN_ID']}))
    emit({'type': 'result', 'subtype': 'success', 'is_error': False})
    sys.exit(0)
# The runner must terminate us at the first error, not wait for a final result.
time.sleep(15)
(root / 'continued-after-error').touch()
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('claude', 'cursor-agent', 'gh', 'git', 'lark-cli'):
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o755)
        (self.root / 'checkout').mkdir()
        (self.root / 'INSTRUCTIONS.md').write_text('{{RUN_ID}} {{FAILURE_FILE}} {{REPO_WHITELIST_TABLE}}')
        (self.root / 'state.json').write_text('{"last_run": "previous"}')
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}',
                        TEST_ROOT=str(self.root), REPO_WHITELIST=f'repo={self.root}/checkout',
                        NOTIFY_USER_ID='test-recipient', REVIEW_MODEL='test-model')

    def invoke(self, scenario, agent='cursor'):
        result = subprocess.run([sys.executable, '-I', str(ROOT / 'runner.py'), str(self.root)],
                                env=dict(self.env, SCENARIO=scenario, REVIEW_AGENT=agent),
                                capture_output=True, text=True, timeout=8)
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        log = next((self.root / 'logs').glob('*.log')).read_text()
        self.assertFalse((self.root / '.lock').exists())
        self.assertFalse((self.root / '.tmp').exists())
        self.assertFalse((self.root / 'continued-after-error').exists())
        return result, calls, log

    def test_success_and_cli_modes(self):
        for agent, option in [('cursor', '--auto-review'), ('claude', 'auto')]:
            with self.subTest(agent=agent):
                (self.root / 'calls.jsonl').write_text('')
                result, calls, _ = self.invoke('success', agent)
                self.assertEqual(result.returncode, 0, result.stderr)
                cli = next(c for c in calls if c['name'] in ('claude', 'cursor-agent'))
                self.assertIn(option, cli['args'])
                self.assertIn('test-model', cli['args'])
                self.assertNotIn('--force', cli['args'])
                self.assertNotIn('--allowedTools', cli['args'])
                self.assertFalse(any(c['name'] == 'lark-cli' for c in calls))

    def test_failures_stop_and_notify_once(self):
        cases = [('tool_error', 'This command requires approval'),
                 ('cursor_error', 'cargo: command not found'),
                 ('cursor_denied', 'read blocked'),
                 ('cursor_subagent', 'Subagent delegation is disabled'),
                 ('command_error', 'invalid command arguments'),
                 ('api_error', 'rate limit exceeded'),
                 ('marker', 'diff is incomplete'),
                 ('exit_error', 'backend unavailable'),
                 ('missing_result', 'without a successful result'),
                 ('missing_state', 'did not complete state.last_run'),
                 ('preflight', 'GitHub authentication expired')]
        for scenario, reason in cases:
            with self.subTest(scenario=scenario):
                (self.root / 'calls.jsonl').write_text('')
                result, calls, log = self.invoke(scenario)
                self.assertEqual(result.returncode, 1, result.stderr)
                notices = [c for c in calls if c['name'] == 'lark-cli']
                self.assertEqual(len(notices), 1)
                self.assertIn(reason, notices[0]['args'][-1])
                self.assertIn(reason, log)
                self.assertLessEqual(sum(c['name'] == 'cursor-agent' for c in calls), 1)
                self.assertEqual(json.loads((self.root / 'state.json').read_text())['last_run'], 'previous')

    def test_empty_shell_search_completes_for_both_agents(self):
        for agent in ('claude', 'cursor'):
            for command in ('grep', 'rg'):
                with self.subTest(agent=agent, command=command):
                    self.env['SEARCH_COMMAND'] = command
                    (self.root / 'calls.jsonl').write_text('')
                    result, calls, _ = self.invoke('search_empty', agent)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(any(c['name'] == 'lark-cli' for c in calls))

    def test_quoted_and_escaped_search_symbols_are_arguments(self):
        patterns = [('grep', ';', "';'"), ('rg -F', '>', "'>'"),
                    ('grep', ';', r'\;'), ('rg -F', '>', r'\>'),
                    ('grep', '`', "'`'"), ('rg -F', '$(', "'$('"),
                    ('grep', ';', '";"')]
        for agent in ('claude', 'cursor'):
            for command, pattern, shell_pattern in patterns:
                with self.subTest(agent=agent, command=command, pattern=shell_pattern):
                    self.env.update(SEARCH_COMMAND=command, SEARCH_PATTERN=pattern,
                                    SEARCH_SHELL_PATTERN=shell_pattern)
                    (self.root / 'calls.jsonl').write_text('')
                    result, calls, _ = self.invoke('search_empty', agent)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(any(c['name'] == 'lark-cli' for c in calls))

    def test_search_errors_and_compound_failures_still_abort(self):
        cases = [('search_error', ''), ('search_compound', ' && false'),
                 ('search_compound', '; false'), ('search_compound', ' | false'),
                 ('search_compound', ' > output')]
        for agent in ('claude', 'cursor'):
            for scenario, suffix in cases:
                with self.subTest(agent=agent, scenario=scenario, suffix=suffix):
                    self.env['SEARCH_SUFFIX'] = suffix
                    (self.root / 'calls.jsonl').write_text('')
                    result, calls, _ = self.invoke(scenario, agent)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    notices = [c for c in calls if c['name'] == 'lark-cli']
                    self.assertEqual(len(notices), 1)
                    self.assertIn('grep', notices[0]['args'][-1])

    def test_notification_failure_is_logged(self):
        for scenario, reason in [('notify_error', 'Lark token expired'), ('notify_rejected', 'recipient unavailable')]:
            with self.subTest(scenario=scenario):
                (self.root / 'calls.jsonl').write_text('')
                result, calls, log = self.invoke(scenario)
                self.assertEqual(result.returncode, 1)
                self.assertIn('Failure notification failed', log)
                self.assertIn(reason, log)
                self.assertEqual(sum(c['name'] == 'lark-cli' for c in calls), 1)

    def test_failure_stops_detached_shell_child(self):
        result, _, _ = self.invoke('child_error')
        self.assertEqual(result.returncode, 1)
        pid = (self.root / 'child.pid').read_text()
        status = subprocess.run(['ps', '-p', pid, '-o', 'stat='], capture_output=True, text=True)
        self.assertTrue(status.returncode != 0 or 'Z' in status.stdout, status.stdout)


if __name__ == '__main__':
    unittest.main()
