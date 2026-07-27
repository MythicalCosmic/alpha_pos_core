import logging
import requests

logger = logging.getLogger(__name__)

MAX_REQUEST_TIMEOUT_SECONDS = 20


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
        request_timeout = min(
            MAX_REQUEST_TIMEOUT_SECONDS,
            max(1, int(config.timeout or 1)),
        )

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
                resp = requests.post(
                    url,
                    json=payload,
                    timeout=request_timeout,
                )
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
    def edit_in_chats(cls, text, message_ids, chat_ids=None):
        """Edit one previously sent message in each target chat.

        ``message_ids`` is the ``{"<chat_id>": <telegram_message_id>}`` map
        captured from ``send_to_chats``.  Returns ``(failed_chat_ids,
        last_error)`` so the queue can retry only the chats whose edit did not
        land, without creating a second order notification.

        Telegram returns HTTP 400 when a retry submits text that is already on
        the message.  Treat that specific response as success: the requested
        final state is already visible and another retry would never help.
        """
        config = cls._get_config()
        target_values = (
            chat_ids if chat_ids is not None else (message_ids or {}).keys()
        )
        targets = list(dict.fromkeys(
            str(chat_id).strip()
            for chat_id in target_values
            if chat_id is not None and str(chat_id).strip()
        ))
        if not config.bot_token:
            return targets, 'Bot token not configured'

        url = f'https://api.telegram.org/bot{config.bot_token}/editMessageText'
        token = config.bot_token
        known_ids = {
            str(chat_id): message_id
            for chat_id, message_id in (message_ids or {}).items()
        }
        failed = []
        last_error = ''
        request_timeout = min(
            MAX_REQUEST_TIMEOUT_SECONDS,
            max(1, int(config.timeout or 1)),
        )

        for chat_id in targets:
            message_id = known_ids.get(chat_id)
            if message_id is None:
                failed.append(chat_id)
                last_error = 'Original order message is not available yet'
                continue

            payload = {
                'chat_id': chat_id,
                'message_id': message_id,
                'text': text,
                'parse_mode': 'HTML',
            }
            try:
                resp = requests.post(
                    url,
                    json=payload,
                    timeout=request_timeout,
                )
                if resp.ok or cls._is_message_not_modified(resp):
                    continue
                failed.append(chat_id)
                last_error = (
                    f'HTTP {resp.status_code}: '
                    f'{_redact(resp.text[:200], token)}'
                )
                logger.warning(
                    'Telegram edit API error for %s: %s',
                    chat_id,
                    resp.status_code,
                )
            except requests.ConnectionError:
                failed.append(chat_id)
                last_error = 'Connection error'
                logger.warning('Telegram edit connection error for %s', chat_id)
            except requests.Timeout:
                failed.append(chat_id)
                last_error = 'Timeout'
                logger.warning('Telegram edit timeout for %s', chat_id)
            except Exception as exc:
                failed.append(chat_id)
                last_error = _redact(str(exc), token)
                logger.error(
                    'Telegram edit error for %s: %s',
                    chat_id,
                    last_error,
                )

        return failed, last_error

    @staticmethod
    def _is_message_not_modified(response):
        """True when Telegram confirms an idempotent edit already landed."""
        if response.status_code != 400:
            return False
        try:
            description = str((response.json() or {}).get('description') or '')
        except (ValueError, AttributeError):
            description = str(getattr(response, 'text', '') or '')
        return 'message is not modified' in description.lower()

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
