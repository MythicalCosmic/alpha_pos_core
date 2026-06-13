from base.helpers.response import ServiceResponse
from notifications.models import NotificationSettings, NotificationTemplate


class ConfigService:
    @classmethod
    def load(cls):
        return NotificationSettings.load()

    @classmethod
    def is_enabled(cls, notification_type=None):
        settings = cls.load()
        if not settings.is_enabled:
            return False
        if notification_type:
            template = NotificationTemplate.objects.filter(
                notification_type=notification_type
            ).first()
            if template and not template.is_enabled:
                return False
        return True

    @classmethod
    def get_settings(cls):
        s = cls.load()
        templates = NotificationTemplate.objects.all()
        return ServiceResponse.success(data={
            'brand_name': s.brand_name,
            'is_enabled': s.is_enabled,
            'bot_configured': bool(s.bot_token),
            'chat_ids': s.chat_ids,
            'timeout': s.timeout,
            'types': [{'type': t.notification_type, 'name': t.name, 'is_enabled': t.is_enabled} for t in templates],
        })

    @classmethod
    def update_settings(cls, **kwargs):
        s = cls.load()
        allowed = {'bot_token', 'chat_ids', 'brand_name', 'is_enabled', 'timeout'}
        for k, v in kwargs.items():
            if k in allowed:
                setattr(s, k, v)
        s.save()
        return cls.get_settings()

    @classmethod
    def enable(cls, notification_type=None):
        if notification_type:
            NotificationTemplate.objects.filter(notification_type=notification_type).update(is_enabled=True)
        else:
            s = cls.load()
            s.is_enabled = True
            s.save(update_fields=['is_enabled'])
        return ServiceResponse.success(message='Enabled')

    @classmethod
    def disable(cls, notification_type=None):
        if notification_type:
            NotificationTemplate.objects.filter(notification_type=notification_type).update(is_enabled=False)
        else:
            s = cls.load()
            s.is_enabled = False
            s.save(update_fields=['is_enabled'])
        return ServiceResponse.success(message='Disabled')

    @classmethod
    def get_status(cls):
        from notifications.services.telegram_service import TelegramService
        from notifications.services.queue_service import QueueService
        return ServiceResponse.success(data={
            'is_enabled': cls.load().is_enabled,
            'bot_online': TelegramService.is_online(),
            'queue_count': QueueService.count(),
        })
