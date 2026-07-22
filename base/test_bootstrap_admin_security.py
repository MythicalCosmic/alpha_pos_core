import io
import sys

import pytest
from django.core.management import call_command

from base.models import User
from base.security.hashing import verify_password


@pytest.mark.django_db
def test_bootstrap_admin_never_logs_supplied_password(monkeypatch, capsys):
    supplied = 'deploy-secret-that-must-not-appear'
    monkeypatch.setenv('ALPHA_POS_ADMIN_EMAIL', 'secure-admin@example.com')
    monkeypatch.setenv('ALPHA_POS_ADMIN_PASSWORD', supplied)

    stderr = io.StringIO()
    call_command('bootstrap_admin', stdout=io.StringIO(), stderr=stderr)

    captured = capsys.readouterr()
    combined = captured.out + captured.err + stderr.getvalue()
    assert supplied not in combined
    assert 'supplied securely (not printed)' in combined

    user = User.objects.get(email='secure-admin@example.com')
    assert verify_password(supplied, user.password)


@pytest.mark.django_db
def test_bootstrap_admin_accepts_supplied_password_without_gui_console(
    monkeypatch,
):
    supplied = 'desktop-generated-secure-password'
    monkeypatch.setattr(sys, 'stderr', None)

    call_command(
        'bootstrap_admin', email='admin@local', password=supplied,
        stdout=io.StringIO(), verbosity=0,
    )

    user = User.objects.get(email='admin@local')
    assert verify_password(supplied, user.password)


@pytest.mark.django_db
def test_bootstrap_admin_refuses_inaccessible_generated_password(monkeypatch):
    from django.core.management.base import CommandError

    monkeypatch.delenv('ALPHA_POS_ADMIN_PASSWORD', raising=False)
    monkeypatch.setattr(sys, 'stderr', None)

    with pytest.raises(CommandError, match='without a console'):
        call_command('bootstrap_admin', stdout=io.StringIO(), verbosity=0)
    assert not User.objects.exists()
