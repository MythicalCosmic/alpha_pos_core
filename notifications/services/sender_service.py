import html
import logging
from notifications.models import NotificationSettings, NotificationTemplate
from notifications.services.safe_format import safe_format, _UnsafePlaceholder

logger = logging.getLogger(__name__)


def _escape_context(context):
    # Escape every string value so user-controlled fields (cashier names,
    # product names, customer phones) cannot break Telegram's HTML parser
    # or inject markup. Templates may still contain literal <b>, <i>, etc.
    out = {}
    for k, v in context.items():
        if isinstance(v, str):
            out[k] = html.escape(v, quote=False)
        else:
            out[k] = v
    return out


class SenderService:
    @classmethod
    def send(cls, notification_type, context, order_id=None, thread_role=None):
        """Main send API. Resolves template, checks enabled, sends async.

        order_id + thread_role enable order-notification reply threading (see
        notifications.services.worker.enqueue).

        Returns True only when work was actually enqueued.  Existing callers
        may continue ignoring the result; lifecycle dispatchers use it to avoid
        marking a creation/READY transition sent when configuration suppressed
        it or no creation recipients exist.
        """
        from notifications.services.config_service import ConfigService
        if not ConfigService.is_enabled(notification_type):
            return False

        template = NotificationTemplate.objects.filter(
            notification_type=notification_type
        ).first()
        if not template:
            logger.warning(f'No template for {notification_type}')
            return False

        settings = NotificationSettings.load()
        # A READY edit resolves its frozen creation audience inside the worker,
        # so current routing is intentionally irrelevant there.  A creation,
        # however, must not be marked sent if there is nobody to receive/store.
        if thread_role == 'new' and not settings.recipients_for(notification_type):
            logger.info(
                'No recipients for %s; lifecycle creation remains pending',
                notification_type,
            )
            return False
        context['brand'] = settings.brand_name

        try:
            text = safe_format(template.template_text, **_escape_context(context))
        except (KeyError, IndexError) as e:
            logger.error(f'Template render error for {notification_type}: {e}')
            return False
        except _UnsafePlaceholder as e:
            # Stored template tried to reach inside an object (e.g. via the
            # str.format `{x.__class__}` trick). Drop the notification and
            # surface loudly — the template needs to be fixed by an admin.
            logger.error(
                'unsafe placeholder in template %s: %s', notification_type, e,
            )
            return False

        cls._send_async(text, notification_type, order_id=order_id, thread_role=thread_role)
        return True

    @classmethod
    def send_raw(cls, text):
        """Send arbitrary text (for test messages)."""
        cls._send_async(text, 'test')
        return True

    @classmethod
    def _send_async(cls, text, notification_type, order_id=None, thread_role=None):
        # Hand off to the single background worker which serializes sends and
        # enforces a per-chat minimum interval. Avoids spawning a thread per
        # message under burst load.
        from notifications.services.worker import enqueue
        enqueue(text, notification_type, order_id=order_id, thread_role=thread_role)
        return True
