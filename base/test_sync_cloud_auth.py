import json
import uuid

import pytest
from django.test import RequestFactory


def _receive(*, token='cloud-secret', branch_header=None):
    headers = {'HTTP_AUTHORIZATION': f'Cloud {token}'}
    if branch_header is not None:
        headers['HTTP_X_BRANCH_ID'] = branch_header
    request = RequestFactory().post(
        '/api/sync/receive',
        data=json.dumps({
            'model': 'order',
            'records': [{'uuid': str(uuid.uuid4()), 'sync_version': 1}],
        }),
        content_type='application/json',
        **headers,
    )
    from base.services.sync.views import receive
    response = receive(request)
    return response.status_code, json.loads(response.content)


def test_cloud_credential_is_refused_on_cloud_node(settings, monkeypatch):
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.CLOUD_SYNC_TOKEN = 'cloud-secret'
    settings.BRANCH_ID = 'cloud'
    called = []
    monkeypatch.setattr(
        CloudReceiver, 'receive_batch',
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    status, body = _receive(branch_header='branch-a')

    assert status == 403
    assert 'only on local nodes' in body['error']
    assert called == []


@pytest.mark.parametrize('configured,header', [
    ('branch-a', None),
    ('branch-a', 'branch-b'),
    ('', 'branch-a'),
])
def test_cloud_credential_requires_exact_local_branch(
    settings, monkeypatch, configured, header,
):
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'local'
    settings.CLOUD_SYNC_TOKEN = 'cloud-secret'
    settings.BRANCH_ID = configured
    called = []
    monkeypatch.setattr(
        CloudReceiver, 'receive_batch',
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    status, body = _receive(branch_header=header)

    assert status == 403
    assert 'must match this local node' in body['error']
    assert called == []


def test_cloud_credential_accepts_exact_local_target(settings, monkeypatch):
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'local'
    settings.CLOUD_SYNC_TOKEN = 'cloud-secret'
    settings.BRANCH_ID = 'branch-a'
    calls = []

    def fake_receive(model_name, branch_id, records):
        calls.append((model_name, branch_id, records))
        return {
            'success': True, 'created': 1, 'updated': 0,
            'skipped': 0, 'errors': [], 'failed_uuids': [],
        }

    monkeypatch.setattr(CloudReceiver, 'receive_batch', fake_receive)

    status, body = _receive(branch_header='branch-a')

    assert status == 200
    assert body['success'] is True
    assert calls[0][0:2] == ('order', 'branch-a')
