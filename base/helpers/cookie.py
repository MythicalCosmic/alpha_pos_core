from django.conf import settings


def set_session_cookie(response, session_key):
    secure = getattr(settings, 'SESSION_COOKIE_SECURE', False)
    response.set_cookie(
        'session_key',
        session_key,
        httponly=True,
        samesite='Lax',
        secure=secure,
        max_age=86400 * 7,
    )


def clear_session_cookie(response):
    response.delete_cookie('session_key')
