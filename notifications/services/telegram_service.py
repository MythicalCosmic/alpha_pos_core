import logging
import requests

logger = logging.getLogger(__name__)


def _redact(text, secret):
    # Strip the bot token from any string that may include it (Telegram URLs
    # contain the token in the path, and some error paths echo the URL).
    if not secret or not text:
        return text
    return str(text).replace(secret, '<REDACTED>')


class TelegramService:
    @classmethod
    def _get_config(cls):
        from notifications.models import NotificationSettings
        return NotificationSettings.load()

    @classmethod
    def send_to_chats(cls, text, chat_ids):
        """Send `text` to each chat. Returns (failed_chat_ids, last_error).

        Returning the specific chats that failed (rather than one aggregate
        bool) lets the retry path re-send ONLY to those chats — otherwise a
        single failing chat causes the whole message to be re-queued and the
        chats that already received it get duplicates on every retry."""
        config = cls._get_config()
        if not config.bot_token:
            return list(chat_ids), 'Bot token not configured'

        url = f'https://api.telegram.org/bot{config.bot_token}/sendMessage'
        token = config.bot_token
        failed = []
        last_error = ''

        for chat_id in chat_ids:
            try:
                resp = requests.post(url, json={
                    'chat_id': chat_id,
                    'text': text,
                    'parse_mode': 'HTML',
                }, timeout=config.timeout)
                if not resp.ok:
                    failed.append(chat_id)
                    # Don't return resp.text directly — Telegram error bodies
                    # sometimes echo the request URL (which contains the
                    # bot token). Keep the status code and a redacted snippet.
                    last_error = f'HTTP {resp.status_code}: {_redact(resp.text[:200], token)}'
                    logger.warning(f'Telegram API error for {chat_id}: {resp.status_code}')
            except requests.ConnectionError:
                failed.append(chat_id)
                last_error = 'Connection error'
                logger.warning(f'Telegram connection error for {chat_id}')
            except requests.Timeout:
                failed.append(chat_id)
                last_error = 'Timeout'
                logger.warning(f'Telegram timeout for {chat_id}')
            except Exception as e:
                failed.append(chat_id)
                last_error = _redact(str(e), token)
                logger.error(f'Telegram error for {chat_id}: {last_error}')

        return failed, last_error

    @classmethod
    def send_message(cls, text, chat_ids=None):
        """Back-compat wrapper returning (success, last_error). When chat_ids is
        None, sends to every configured chat."""
        config = cls._get_config()
        targets = chat_ids if chat_ids is not None else config.chat_ids
        if not config.bot_token or not targets:
            return False, 'Bot token or chat IDs not configured'
        failed, last_error = cls.send_to_chats(text, targets)
        return (len(failed) == 0), last_error

    @classmethod
    def is_online(cls):
        config = cls._get_config()
        if not config.bot_token:
            return False
        try:
            url = f'https://api.telegram.org/bot{config.bot_token}/getMe'
            resp = requests.get(url, timeout=5)
            return resp.ok
        except Exception:
            return False
