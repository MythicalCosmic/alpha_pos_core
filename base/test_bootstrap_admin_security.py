import io

import pytest
from django.core.management import call_command

from base.models import User
from base.security.hashing import verify_password


@pytest.mark.django_db
def test_bootstrap_admin_never_logs_supplied_password(monkeypatch, capsys):
    supplied = 'deploy-secret-that-must-not-appear'
    monkeypatch.setenv('ALPHA_POS_ADMIN_EMAIL', 'secure-admin@example.com')
    monkeypatch.setenv('ALPHA_POS_ADMIN_PASSWORD', supplied)

    call_command('bootstrap_admin', stdout=io.StringIO(), stderr=io.StringIO())

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert supplied not in combined
    assert 'supplied securely (not printed)' in combined

    user = User.objects.get(email='secure-admin@example.com')
    assert verify_password(supplied, user.password)
