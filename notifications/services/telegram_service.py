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
    def send_to_chats(cls, text, chat_ids, reply_to=None):
        """Send `text` to each chat. Returns (failed_chat_ids, last_error, sent_ids).

        `sent_ids` is `{"<chat_id>": <telegram_message_id>}` for the chats that
        accepted the message — used to reply-thread a later message under this one.
        `reply_to` (optional) is `{"<chat_id>": <message_id>}`: when present for a
        chat, the message is sent as a reply to that message id (with
        allow_sending_without_reply so a deleted original doesn't fail the send).

        Returning the specific chats that failed (rather than one aggregate bool)
        lets the retry path re-send ONLY to those chats — otherwise a single
        failing chat causes the whole message to be re-queued and the chats that
        already received it get duplicates on every retry."""
        config = cls._get_config()
        if not config.bot_token:
            return list(chat_ids), 'Bot token not configured', {}

        url = f'https://api.telegram.org/bot{config.bot_token}/sendMessage'
        token = config.bot_token
        failed = []
        last_error = ''
        sent_ids = {}

        for chat_id in chat_ids:
            payload = {
                'chat_id': chat_id,
                'text': text,
                'parse_mode': 'HTML',
            }
            reply_mid = (reply_to or {}).get(str(chat_id))
            if reply_mid:
                payload['reply_to_message_id'] = reply_mid
                payload['allow_sending_without_reply'] = True
            try:
                resp = requests.post(url, json=payload, timeout=config.timeout)
                if resp.ok:
                    try:
                        mid = (resp.json() or {}).get('result', {}).get('message_id')
                        if mid is not None:
                            sent_ids[str(chat_id)] = mid
                    except (ValueError, AttributeError):
                        pass
                else:
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

        return failed, last_error, sent_ids

    @classmethod
    def send_message(cls, text, chat_ids=None):
        """Back-compat wrapper returning (success, last_error). When chat_ids is
        None, sends to every configured chat."""
        config = cls._get_config()
        targets = chat_ids if chat_ids is not None else config.chat_ids
        if not config.bot_token or not targets:
            return False, 'Bot token or chat IDs not configured'
        failed, last_error, _ = cls.send_to_chats(text, targets)
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
