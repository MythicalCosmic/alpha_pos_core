from django.core.cache import cache
from base.notifications.helpers import uzb_now, format_duration_minutes

SESSION_KEY = 'notif:shift:session'
SESSION_TTL = 86400


class ShiftSession:

    @classmethod
    def get(cls):
        return cache.get(SESSION_KEY)

    @classmethod
    def start(cls, user_id, user_name):
        session = {
            'user_id': user_id,
            'user_name': user_name,
            'login_time': uzb_now().isoformat(),
        }
        cache.set(SESSION_KEY, session, SESSION_TTL)
        return session

    @classmethod
    def clear(cls):
        old = cls.get()
        cache.delete(SESSION_KEY)
        return old

    @classmethod
    def get_info(cls):
        session = cls.get()
        if not session:
            return None

        from base.notifications.helpers import UZB_TZ
        from datetime import datetime
        start = datetime.fromisoformat(session['login_time'])
        if start.tzinfo is None:
            start = start.replace(tzinfo=UZB_TZ)

        now = uzb_now()
        duration_min = int((now - start).total_seconds() / 60)

        return {
            'user_id': session['user_id'],
            'user_name': session['user_name'],
            'login_time': session['login_time'],
            'duration': format_duration_minutes(duration_min),
            'duration_minutes': duration_min,
        }
