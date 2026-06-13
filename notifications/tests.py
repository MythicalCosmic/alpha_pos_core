"""Regression tests for the editable notification template API."""
import json

import pytest
from django.test import Client

from base.models import Session
from base.repositories.session import SessionRepository
from notifications.models import NotificationTemplate


pytestmark = pytest.mark.django_db


def _admin_client(user):
    """Build a Client authenticated as `user` via the same cookie-session
    flow real callers use. Mirrors what Postman does."""
    import secrets
    from django.utils import timezone
    from datetime import timedelta
    token = secrets.token_hex(32)
    Session.objects.create(
        user_id=user,
        ip_address='127.0.0.1',
        user_agent='pytest',
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(days=1),
    )
    client = Client(HTTP_USER_AGENT='pytest')
    client.cookies['session_key'] = token
    return client


@pytest.fixture
def template(db):
    return NotificationTemplate.objects.create(
        notification_type='test.greeting',
        name='Greeting',
        template_text='Hello {name}, welcome to {brand}',
    )


class TestTemplateCreate:
    def test_create_minimum_fields(self, admin_user):
        client = _admin_client(admin_user)
        resp = client.post(
            '/api/admins/notifications/templates/',
            data=json.dumps({
                'notification_type': 'telegram.start',
                'name': 'Start command',
                'template_text': 'Welcome to {brand}',
            }),
            content_type='application/json',
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body['data']['notification_type'] == 'telegram.start'
        assert NotificationTemplate.objects.filter(notification_type='telegram.start').exists()

    def test_duplicate_type_rejected(self, admin_user, template):
        client = _admin_client(admin_user)
        resp = client.post(
            '/api/admins/notifications/templates/',
            data=json.dumps({
                'notification_type': template.notification_type,
                'name': 'Dup',
                'template_text': 'x',
            }),
            content_type='application/json',
        )
        assert resp.status_code == 409

    def test_missing_fields_422(self, admin_user):
        client = _admin_client(admin_user)
        resp = client.post(
            '/api/admins/notifications/templates/',
            data=json.dumps({'notification_type': 'x.y'}),
            content_type='application/json',
        )
        assert resp.status_code == 422

    def test_positional_placeholder_rejected(self, admin_user):
        client = _admin_client(admin_user)
        resp = client.post(
            '/api/admins/notifications/templates/',
            data=json.dumps({
                'notification_type': 'x.y',
                'name': 'X',
                'template_text': 'Hello {}',
            }),
            content_type='application/json',
        )
        assert resp.status_code == 422


class TestTemplateDelete:
    def test_delete(self, admin_user, template):
        client = _admin_client(admin_user)
        resp = client.delete(f'/api/admins/notifications/templates/{template.id}/')
        assert resp.status_code == 200
        assert not NotificationTemplate.objects.filter(id=template.id).exists()

    def test_delete_missing_404(self, admin_user):
        client = _admin_client(admin_user)
        resp = client.delete('/api/admins/notifications/templates/999999/')
        assert resp.status_code == 404


class TestTemplatePreview:
    def test_preview_renders(self, admin_user, template):
        client = _admin_client(admin_user)
        resp = client.post(
            f'/api/admins/notifications/templates/{template.id}/preview/',
            data=json.dumps({'context': {'name': 'Adrian'}}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        body = resp.json()
        # brand falls back to NotificationSettings.brand_name (default 'Alpha POS')
        assert 'Hello Adrian' in body['data']['rendered']
        assert 'Alpha POS' in body['data']['rendered']

    def test_preview_escapes_html_input(self, admin_user, template):
        client = _admin_client(admin_user)
        resp = client.post(
            f'/api/admins/notifications/templates/{template.id}/preview/',
            data=json.dumps({'context': {'name': '<script>x</script>'}}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        rendered = resp.json()['data']['rendered']
        assert '<script>' not in rendered
        assert '&lt;script&gt;' in rendered

    def test_preview_missing_context_key_422(self, admin_user, template):
        client = _admin_client(admin_user)
        # No 'name' provided; template needs it.
        resp = client.post(
            f'/api/admins/notifications/templates/{template.id}/preview/',
            data=json.dumps({'context': {}}),
            content_type='application/json',
        )
        assert resp.status_code == 422
        assert 'name' in resp.json()['message']


class TestTemplateListFilters:
    def test_filter_by_type(self, admin_user, template):
        NotificationTemplate.objects.create(
            notification_type='other.type', name='Other', template_text='x',
        )
        client = _admin_client(admin_user)
        resp = client.get('/api/admins/notifications/templates/?notification_type=test.greeting')
        assert resp.status_code == 200
        items = resp.json()['data']
        assert len(items) == 1
        assert items[0]['notification_type'] == 'test.greeting'


class TestNonAdminBlocked:
    def test_regular_user_cannot_create(self, regular_user):
        client = _admin_client(regular_user)
        resp = client.post(
            '/api/admins/notifications/templates/',
            data=json.dumps({
                'notification_type': 'x.y',
                'name': 'X',
                'template_text': 'x',
            }),
            content_type='application/json',
        )
        assert resp.status_code == 403


class TestTemplateSandbox:
    """str.format with admin-controlled templates is famously unsafe:
    `{x.__class__.__init__.__globals__[os].environ}` reads process env.
    The safe formatter must refuse any placeholder with `.` or `[`."""

    def test_safe_format_blocks_attribute_access(self):
        from notifications.services.safe_format import safe_format, _UnsafePlaceholder

        with pytest.raises(_UnsafePlaceholder):
            safe_format('hello {brand.__class__}', brand='Alpha')

    def test_safe_format_blocks_index_access(self):
        from notifications.services.safe_format import safe_format, _UnsafePlaceholder

        with pytest.raises(_UnsafePlaceholder):
            safe_format('hello {brand[0]}', brand='Alpha')

    def test_safe_format_allows_named_placeholder(self):
        from notifications.services.safe_format import safe_format

        assert safe_format('hello {name}', name='world') == 'hello world'

    def test_validator_refuses_attribute_template(self, admin_user):
        client = _admin_client(admin_user)
        resp = client.post(
            '/api/admins/notifications/templates/',
            data=json.dumps({
                'notification_type': 'x.attr_attempt',
                'name': 'X',
                'template_text': 'pwn {brand.__class__}',
            }),
            content_type='application/json',
        )
        assert resp.status_code == 422
        assert 'attribute' in resp.json()['message']
