import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings


def _settings_process(tmp_path, **overrides):
    env = os.environ.copy()
    for key in (
        'DJANGO_SETTINGS_MODULE',
        'USE_REDIS',
        'USE_DUMMY_CACHE',
        'WEB_CONCURRENCY',
        'GUNICORN_WORKERS',
    ):
        env.pop(key, None)
    env.update({
        'ALPHA_POS_DATA_DIR': str(tmp_path),
        'DEBUG': 'False',
        'PYTHONPATH': os.pathsep.join(filter(None, (
            str(Path(__file__).resolve().parents[3]),
            env.get('PYTHONPATH'),
        ))),
        'SECRET_KEY': 'settings-contract-secret',
        **overrides,
    })
    return subprocess.run(
        [sys.executable, '-c', 'import alpha_pos_core.settings_base'],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_middleware_dependencies_remain_ordered():
    middleware = settings.MIDDLEWARE

    assert middleware.index(
        'django.middleware.security.SecurityMiddleware',
    ) < middleware.index('whitenoise.middleware.WhiteNoiseMiddleware')
    assert middleware.index(
        'django.contrib.sessions.middleware.SessionMiddleware',
    ) < middleware.index('django.contrib.auth.middleware.AuthenticationMiddleware')
    assert middleware.index(
        'django.contrib.auth.middleware.AuthenticationMiddleware',
    ) < middleware.index('django.contrib.messages.middleware.MessageMiddleware')


def test_dummy_cache_is_rejected_in_production(tmp_path):
    result = _settings_process(tmp_path, USE_DUMMY_CACHE='True')

    assert result.returncode != 0
    assert 'USE_DUMMY_CACHE is test-only' in result.stderr


def test_locmem_warning_uses_uvicorn_worker_count(tmp_path):
    result = _settings_process(
        tmp_path,
        DEPLOYMENT_MODE='cloud',
        WEB_CONCURRENCY='4',
    )

    assert result.returncode == 0, result.stderr
    assert '4 Uvicorn workers' in result.stderr
