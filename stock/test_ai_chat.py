"""AIChatService: persisted, multi-turn AI conversations (history)."""
import pytest

from stock.services import ai_chat_service
from stock.services.ai_chat_service import AIChatService
from stock.models import AIChat, AIMessage


def _patch_pq(monkeypatch, fn):
    # Replace the classmethod with a plain function so the service's
    # AIStockAssistant.process_query(query, ...) call hits our stub (no LLM).
    monkeypatch.setattr(ai_chat_service.AIStockAssistant, 'process_query', fn)


@pytest.mark.django_db
def test_send_creates_chat_and_saves_both_messages(monkeypatch):
    seen = {}

    def fake(query, user_id=None, location_id=None, history=None):
        seen['history'] = history
        return {'success': True, 'response': 'ANSWER', 'suggestions': []}

    _patch_pq(monkeypatch, fake)
    r = AIChatService.send(user_id=7, query='sales today?')
    assert r['success'] and r['response'] == 'ANSWER'
    assert seen['history'] == []                       # new chat -> no history
    chat = AIChat.objects.get(id=r['chat_id'])
    assert chat.user_id == 7 and chat.title == 'sales today?'
    assert [m.role for m in chat.messages.order_by('id')] == ['user', 'assistant']
    assert chat.messages.order_by('id')[0].content == 'sales today?'
    assert chat.messages.order_by('id')[1].content == 'ANSWER'


@pytest.mark.django_db
def test_send_continues_chat_with_history(monkeypatch):
    _patch_pq(monkeypatch, lambda query, user_id=None, location_id=None, history=None:
              {'success': True, 'response': 'first answer'})
    cid = AIChatService.send(user_id=7, query='q1')['chat_id']

    seen = {}

    def second(query, user_id=None, location_id=None, history=None):
        seen['history'] = history
        return {'success': True, 'response': 'second answer'}

    _patch_pq(monkeypatch, second)
    r2 = AIChatService.send(user_id=7, query='q2', chat_id=cid)
    assert r2['chat_id'] == cid                          # same thread continued
    assert seen['history'] == [
        {'role': 'user', 'content': 'q1'},
        {'role': 'assistant', 'content': 'first answer'},
    ]
    assert AIChat.objects.get(id=cid).messages.count() == 4


@pytest.mark.django_db
def test_rate_limited_does_not_persist(monkeypatch):
    _patch_pq(monkeypatch, lambda query, user_id=None, location_id=None, history=None:
              {'success': False, 'error': 'rate_limited', 'response': 'quota'})
    r = AIChatService.send(user_id=7, query='hi')
    assert r['error'] == 'rate_limited'
    assert AIChat.objects.count() == 0 and AIMessage.objects.count() == 0


@pytest.mark.django_db
def test_error_turn_in_existing_chat_excluded_from_history(monkeypatch):
    # A successful first turn establishes the chat.
    _patch_pq(monkeypatch, lambda query, user_id=None, location_id=None, history=None:
              {'success': True, 'response': 'hello'})
    cid = AIChatService.send(user_id=7, query='hi')['chat_id']

    # A failed turn IN the existing chat is recorded (is_error=True).
    _patch_pq(monkeypatch, lambda query, user_id=None, location_id=None, history=None:
              {'success': False, 'error': 'internal_error', 'response': 'unavailable'})
    AIChatService.send(user_id=7, query='boom', chat_id=cid)
    assert AIMessage.objects.filter(chat_id=cid, role='assistant', is_error=True).exists()

    # The next successful turn replays only the good pair — the whole failed
    # exchange (user 'boom' + error answer) drops out, so history stays clean
    # alternating pairs with no orphan user turn (no consecutive same-role bug).
    seen = {}

    def ok(query, user_id=None, location_id=None, history=None):
        seen['history'] = history
        return {'success': True, 'response': 'ok now'}

    _patch_pq(monkeypatch, ok)
    AIChatService.send(user_id=7, query='again', chat_id=cid)
    assert seen['history'] == [
        {'role': 'user', 'content': 'hi'},
        {'role': 'assistant', 'content': 'hello'},
    ]


@pytest.mark.django_db
def test_failed_first_message_does_not_create_chat(monkeypatch):
    _patch_pq(monkeypatch, lambda query, user_id=None, location_id=None, history=None:
              {'success': False, 'error': 'internal_error', 'response': 'down'})
    r = AIChatService.send(user_id=7, query='hi')
    assert r['error'] == 'internal_error'
    assert AIChat.objects.count() == 0          # no junk chat from a failed first message


@pytest.mark.django_db
def test_quota_exceeded_does_not_persist(monkeypatch):
    _patch_pq(monkeypatch, lambda query, user_id=None, location_id=None, history=None:
              {'success': False, 'error': 'quota_exceeded', 'response': 'rate limited'})
    AIChatService.send(user_id=7, query='hi')
    assert AIChat.objects.count() == 0 and AIMessage.objects.count() == 0


@pytest.mark.django_db
def test_blank_success_answer_not_stored(monkeypatch):
    # A success result with an empty answer (e.g. a truncated model reply) must
    # not leave a blank assistant bubble / junk chat.
    _patch_pq(monkeypatch, lambda query, user_id=None, location_id=None, history=None:
              {'success': True, 'response': '   '})
    AIChatService.send(user_id=7, query='hi')
    assert AIChat.objects.count() == 0 and AIMessage.objects.count() == 0


@pytest.mark.django_db
def test_chats_are_scoped_per_user(monkeypatch):
    _patch_pq(monkeypatch, lambda query, user_id=None, location_id=None, history=None:
              {'success': True, 'response': 'a'})
    a = AIChatService.send(user_id=7, query='mine')['chat_id']
    AIChatService.send(user_id=8, query='theirs')

    chats = AIChatService.list_chats(7)
    assert len(chats) == 1 and chats[0]['id'] == a and chats[0]['message_count'] == 2
    assert AIChatService.get_chat(8, a) is None          # not user 8's chat
    assert AIChatService.delete_chat(8, a) is False
    assert AIChatService.delete_chat(7, a) is True
    assert AIChatService.list_chats(7) == []             # soft-deleted, hidden


@pytest.mark.django_db
def test_bad_chat_id_starts_new_chat(monkeypatch):
    _patch_pq(monkeypatch, lambda query, user_id=None, location_id=None, history=None:
              {'success': True, 'response': 'x'})
    r = AIChatService.send(user_id=7, query='hi', chat_id='not-a-number')
    assert r['chat_id'] and AIChat.objects.filter(id=r['chat_id']).exists()
