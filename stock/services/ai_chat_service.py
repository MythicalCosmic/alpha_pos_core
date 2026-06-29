"""Chat-history orchestration for the AI assistant.

Wraps AIStockAssistant.process_query with persistence so the assistant has memory:
load a conversation's prior turns as context, run the query, and save the user +
assistant messages. The conversation is owned by the operator (`user_id`) who asked.
"""
from django.db import transaction

from stock.models import AIChat, AIMessage
from stock.services.ai_assistant_service import AIStockAssistant

# How many prior turns (user+assistant pairs) to replay as context. Bounds the
# prompt so a long conversation can't blow the token budget.
HISTORY_TURNS = 15

# Error codes that mean no real exchange happened (validation, quota, missing key)
# — never persist these, even into an existing chat; the model produced no answer.
_NO_PERSIST = {'rate_limited', 'invalid_query', 'query_too_long',
               'quota_exceeded', 'no_api_key'}


class AIChatService:
    """Persisted, multi-turn AI conversations."""

    @staticmethod
    def _title_from(query):
        return ' '.join((query or '').split())[:140] or 'New chat'

    @staticmethod
    def _load_chat(user_id, chat_id):
        if not chat_id:
            return None
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return None
        return AIChat.objects.filter(
            id=chat_id, user_id=user_id, is_deleted=False).first()

    @classmethod
    def _history(cls, chat):
        """Prior turns to replay: complete user->assistant PAIRS, oldest first,
        capped to the most recent HISTORY_TURNS pairs. Failed/blank exchanges are
        dropped WHOLE — keeping a failed turn's user question alone would orphan it
        and produce consecutive same-role messages (which confuse, or 400, the
        model). The result is strictly alternating and starts with a user turn."""
        if chat is None:
            return []
        msgs = list(chat.messages.filter(is_deleted=False).order_by('created_at', 'id'))
        pairs, i, n = [], 0, len(msgs)
        while i < n - 1:
            u, a = msgs[i], msgs[i + 1]
            if (u.role == AIMessage.Role.USER and a.role == AIMessage.Role.ASSISTANT
                    and not a.is_error and (a.content or '').strip()):
                pairs.append((u.content, a.content))
                i += 2
            else:
                i += 1  # orphan (error/blank answer, or stray turn) — skip it
        out = []
        for u_content, a_content in pairs[-HISTORY_TURNS:]:
            out.append({'role': 'user', 'content': u_content})
            out.append({'role': 'assistant', 'content': a_content})
        return out

    @classmethod
    def send(cls, user_id, query, chat_id=None, location_id=None, context=None):
        """Run one chat turn: load history for `chat_id` (or start a new chat), call
        the assistant, persist the exchange, and return process_query's result dict
        plus 'chat_id' (and 'chat_title')."""
        chat = cls._load_chat(user_id, chat_id)
        history = cls._history(chat)
        result = AIStockAssistant.process_query(
            query, user_id=user_id, location_id=location_id, history=history,
            context=context,
        )
        if result.get('error') in _NO_PERSIST:
            # Validation / quota / missing key — the model produced no answer.
            if chat is not None:
                result['chat_id'] = chat.id
            return result
        answer = (result.get('response') or '').strip()
        failed = not result.get('success')
        if not answer:
            # Nothing usable to store (blank or truncated answer) — don't persist
            # an empty turn; just surface it.
            if chat is not None:
                result['chat_id'] = chat.id
            return result
        if failed and chat is None:
            # Don't mint a brand-new chat just to record a failure — a fresh query
            # the model couldn't answer leaves no junk thread. (A failure inside an
            # EXISTING chat is still recorded below, in context.)
            return result
        with transaction.atomic():
            if chat is None:
                chat = AIChat.objects.create(
                    user_id=user_id, title=cls._title_from(query))
            AIMessage.objects.create(
                chat=chat, role=AIMessage.Role.USER, content=query)
            AIMessage.objects.create(
                chat=chat, role=AIMessage.Role.ASSISTANT,
                content=result.get('response') or '', is_error=failed,
            )
            chat.save(update_fields=['updated_at'])  # bump recency ordering
        result['chat_id'] = chat.id
        result['chat_title'] = chat.title
        return result

    @classmethod
    def create_chat(cls, user_id, title=''):
        """Create a new empty chat up front so the client can set conversation_id
        BEFORE the first /ai/query/ turn. (The FE's flow is create -> query with that
        id -> fetch.) Empty chats are hidden from list_chats until they hold a
        message, so an abandoned, never-asked chat never shows as a blank sidebar row."""
        chat = AIChat.objects.create(
            user_id=user_id, title=(title or '').strip()[:140] or 'New chat')
        return {
            'id': chat.id,
            'title': chat.title,
            'created_at': chat.created_at.isoformat() if chat.created_at else None,
            'updated_at': chat.updated_at.isoformat() if chat.updated_at else None,
            'preview': '',
            'message_count': 0,
            'messages': [],
        }

    @classmethod
    def list_chats(cls, user_id, limit=50):
        from django.db.models import Count, Q
        chats = (AIChat.objects.filter(user_id=user_id, is_deleted=False)
                 # Hide empty chats (created but never asked) so they don't show as
                 # blank "New chat" rows in the sidebar.
                 .annotate(_n=Count('messages', filter=Q(messages__is_deleted=False)))
                 .filter(_n__gt=0)
                 .order_by('-updated_at')[:max(1, min(int(limit or 50), 200))])
        out = []
        for c in chats:
            last = (c.messages.filter(is_deleted=False)
                    .order_by('-created_at', '-id').first())
            out.append({
                'id': c.id,
                'title': c.title or f'Chat #{c.id}',
                'created_at': c.created_at.isoformat() if c.created_at else None,
                'updated_at': c.updated_at.isoformat() if c.updated_at else None,
                'preview': (last.content[:120] if last else ''),
                'message_count': c._n,
            })
        return out

    @classmethod
    def get_chat(cls, user_id, chat_id):
        chat = cls._load_chat(user_id, chat_id)
        if chat is None:
            return None
        msgs = chat.messages.filter(is_deleted=False).order_by('created_at', 'id')
        return {
            'id': chat.id,
            'title': chat.title or f'Chat #{chat.id}',
            'created_at': chat.created_at.isoformat() if chat.created_at else None,
            'updated_at': chat.updated_at.isoformat() if chat.updated_at else None,
            'messages': [{
                'id': m.id,
                'role': m.role,
                'content': m.content,
                'is_error': m.is_error,
                'created_at': m.created_at.isoformat() if m.created_at else None,
            } for m in msgs],
        }

    @classmethod
    def delete_chat(cls, user_id, chat_id):
        chat = cls._load_chat(user_id, chat_id)
        if chat is None:
            return False
        chat.is_deleted = True
        chat.save(update_fields=['is_deleted', 'updated_at'])
        return True

    @classmethod
    def rename_chat(cls, user_id, chat_id, title):
        chat = cls._load_chat(user_id, chat_id)
        if chat is None:
            return False
        new = (title or '').strip()[:140]
        if not new:
            return False   # empty title is a no-op, not a success
        chat.title = new
        chat.save(update_fields=['title', 'updated_at'])
        return True


__all__ = ['AIChatService']
