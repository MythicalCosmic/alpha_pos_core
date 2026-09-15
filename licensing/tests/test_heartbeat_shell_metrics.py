"""Desktop shell diagnostics included in the license heartbeat metrics."""
import json

from licensing.services import heartbeat

SHELL_ENV = ('ALPHA_POS_SHELL_VERSION', 'ALPHA_POS_WEBVIEW2_VERSION', 'ALPHA_POS_SHELL_STATE_FILE')


def _clear_env(monkeypatch):
    for name in SHELL_ENV:
        monkeypatch.delenv(name, raising=False)


def test_no_shell_environment_adds_nothing(monkeypatch):
    _clear_env(monkeypatch)
    assert heartbeat._desktop_shell_metrics() == {}


def test_versions_and_update_state_are_reported(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    state = tmp_path / 'shell-update-state.json'
    state.write_text(json.dumps({
        'blocked_versions': ['1.1.2'],
        'pending_confirmation': '1.1.3',
        'last_rollback': {
            'from_version': '1.1.2', 'to_version': '1.1.1',
            'reason': 'new version did not confirm: TimedOut', 'at_unix': 1789000000,
        },
    }), encoding='utf-8')
    monkeypatch.setenv('ALPHA_POS_SHELL_VERSION', '1.1.1')
    monkeypatch.setenv('ALPHA_POS_WEBVIEW2_VERSION', '140.0.3485.54')
    monkeypatch.setenv('ALPHA_POS_SHELL_STATE_FILE', str(state))

    assert heartbeat._desktop_shell_metrics() == {
        'version': '1.1.1',
        'webview2_version': '140.0.3485.54',
        'blocked_versions': ['1.1.2'],
        'pending_confirmation': '1.1.3',
        'last_rollback': {
            'from_version': '1.1.2', 'to_version': '1.1.1',
            'reason': 'new version did not confirm: TimedOut', 'at_unix': 1789000000,
        },
    }


def test_values_are_bounded(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({
        'blocked_versions': [f'9.9.{i}' for i in range(50)],
        'last_rollback': {'reason': 'x' * 5000, 'at_unix': 'not-an-int'},
    }), encoding='utf-8')
    monkeypatch.setenv('ALPHA_POS_SHELL_VERSION', 'v' * 500)
    monkeypatch.setenv('ALPHA_POS_SHELL_STATE_FILE', str(state))

    shell = heartbeat._desktop_shell_metrics()
    assert len(shell['version']) == 40
    assert len(shell['blocked_versions']) == 10
    assert len(shell['last_rollback']['reason']) == 200
    assert shell['last_rollback']['at_unix'] is None


def test_missing_malformed_or_oversized_state_is_ignored(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv('ALPHA_POS_SHELL_VERSION', '1.1.0')

    monkeypatch.setenv('ALPHA_POS_SHELL_STATE_FILE', str(tmp_path / 'absent.json'))
    assert heartbeat._desktop_shell_metrics() == {'version': '1.1.0'}

    broken = tmp_path / 'broken.json'
    broken.write_text('{not json', encoding='utf-8')
    monkeypatch.setenv('ALPHA_POS_SHELL_STATE_FILE', str(broken))
    assert heartbeat._desktop_shell_metrics() == {'version': '1.1.0'}

    huge = tmp_path / 'huge.json'
    huge.write_text(json.dumps({'blocked_versions': ['1'] * 40000}), encoding='utf-8')
    monkeypatch.setenv('ALPHA_POS_SHELL_STATE_FILE', str(huge))
    assert heartbeat._desktop_shell_metrics() == {'version': '1.1.0'}

    listing = tmp_path / 'list.json'
    listing.write_text('[1, 2]', encoding='utf-8')
    monkeypatch.setenv('ALPHA_POS_SHELL_STATE_FILE', str(listing))
    assert heartbeat._desktop_shell_metrics() == {'version': '1.1.0'}


def test_collect_metrics_includes_shell_block(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv('ALPHA_POS_SHELL_VERSION', '1.1.0')
    assert heartbeat._collect_metrics()['desktop_shell'] == {'version': '1.1.0'}
