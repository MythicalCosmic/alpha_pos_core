from licensing.services import heartbeat


def test_client_version_prefers_packaged_semantic_version(monkeypatch):
    monkeypatch.setattr(heartbeat, '_CLIENT_VERSION_CACHED', None)
    monkeypatch.setenv('ALPHA_POS_CLIENT_VERSION', 'alpha_pos/1.0.27')
    assert heartbeat._client_version() == 'alpha_pos/1.0.27'


def test_client_version_caps_untrusted_environment_length(monkeypatch):
    monkeypatch.setattr(heartbeat, '_CLIENT_VERSION_CACHED', None)
    monkeypatch.setenv('ALPHA_POS_CLIENT_VERSION', 'v' * 200)
    assert heartbeat._client_version() == 'v' * 100
