"""AI assistant behavior additions: repeat/abuse detection ("annoyed" tone),
the BEHAVIOR directive injection, and the LLM determinism/caching helpers."""
import pytest

from stock.services.ai_assistant_service import AIStockAssistant as A
from stock.services.ai_chat_service import AIChatService
from stock.models import AIChat, AIMessage


# ── _behavior_note (pure) ──────────────────────────────────────────────────

def test_behavior_note_repeat_from_count():
    note = A._behavior_note('sales today', [], repeat_count=1)
    assert note.startswith('BEHAVIOR:') and 'same question' in note and note.endswith('\n\n')


def test_behavior_note_repeat_from_history_fallback():
    # No count passed, but the trailing user turn is the same (normalized) question.
    hist = [{'role': 'user', 'content': 'Sales Today'},
            {'role': 'assistant', 'content': '...'}]
    assert A._behavior_note('sales today', hist, repeat_count=0).startswith('BEHAVIOR:')


def test_behavior_note_abuse():
    note = A._behavior_note('you are stupid', [], 0)
    assert note.startswith('BEHAVIOR:') and 'rude' in note


def test_behavior_note_none_for_fresh_polite_query():
    assert A._behavior_note('what were sales yesterday', [], 0) == ''
    assert A._behavior_note('', [], 5) == ''          # empty query never fires


# ── _repeat_count (DB) ─────────────────────────────────────────────────────

@pytest.mark.django_db
def test_repeat_count_counts_consecutive_normalized_matches():
    chat = AIChat.objects.create(user_id=7, title='t')
    AIMessage.objects.create(chat=chat, role=AIMessage.Role.USER, content='sales today')
    AIMessage.objects.create(chat=chat, role=AIMessage.Role.ASSISTANT, content='a1')
    AIMessage.objects.create(chat=chat, role=AIMessage.Role.USER, content='Sales   Today')
    AIMessage.objects.create(chat=chat, role=AIMessage.Role.ASSISTANT, content='a2')
    # both prior user turns normalize to 'sales today'
    assert AIChatService._repeat_count(chat, 'sales today') == 2
    assert AIChatService._repeat_count(chat, 'something else') == 0
    assert AIChatService._repeat_count(None, 'x') == 0


@pytest.mark.django_db
def test_repeat_count_breaks_on_first_mismatch():
    chat = AIChat.objects.create(user_id=7, title='t')
    for c in ('q1', 'q1', 'other'):   # most-recent-first walk stops at 'other'... wait
        AIMessage.objects.create(chat=chat, role=AIMessage.Role.USER, content=c)
        AIMessage.objects.create(chat=chat, role=AIMessage.Role.ASSISTANT, content='a')
    # prior user turns most-recent-first: other, q1, q1 -> asking 'q1' stops at 'other'
    assert AIChatService._repeat_count(chat, 'q1') == 0
    assert AIChatService._repeat_count(chat, 'other') == 1


# ── process_query injects BEHAVIOR into the user turn (not the system prompt) ─

@pytest.mark.django_db
def test_process_query_injects_behavior_into_user_turn(monkeypatch):
    import base.services.llm as llm
    captured = {}

    monkeypatch.setattr(llm, 'can_use_tools', lambda: False)
    monkeypatch.setattr(A, '_get_all_stock_data', lambda: {})
    monkeypatch.setattr(A, '_get_sales_data', lambda: {})
    monkeypatch.setattr(A, '_needs_analytics', lambda q: False)

    def fake_call_ai(prompt, system=None, max_tokens=None, history=None):
        captured['prompt'] = prompt
        captured['system'] = system
        return 'ok', None

    monkeypatch.setattr(llm, 'call_ai', fake_call_ai)

    A.process_query('sales today', repeat_count=2)
    # BEHAVIOR rides the USER prompt, before the query...
    assert 'BEHAVIOR:' in captured['prompt']
    assert captured['prompt'].index('BEHAVIOR:') < captured['prompt'].index('USER QUERY:')
    # The runtime DIRECTIVE text rides the USER turn only. (The static system
    # prompt documents the "BEHAVIOR:" convention, so assert on the runtime-only
    # phrase, which must never appear in the cached system prefix.)
    assert 'asking the exact same question again' in captured['prompt']
    assert 'asking the exact same question again' not in (captured['system'] or '')


@pytest.mark.django_db
def test_process_query_no_behavior_when_fresh(monkeypatch):
    import base.services.llm as llm
    captured = {}
    monkeypatch.setattr(llm, 'can_use_tools', lambda: False)
    monkeypatch.setattr(A, '_get_all_stock_data', lambda: {})
    monkeypatch.setattr(A, '_get_sales_data', lambda: {})
    monkeypatch.setattr(A, '_needs_analytics', lambda q: False)

    def fake_call_ai(prompt, system=None, max_tokens=None, history=None):
        captured['prompt'] = prompt
        return 'ok', None

    monkeypatch.setattr(llm, 'call_ai', fake_call_ai)
    A.process_query('what were sales yesterday', repeat_count=0)
    assert 'BEHAVIOR:' not in captured['prompt']


# ── System prompt hardening blocks are present ─────────────────────────────

def test_system_prompt_has_security_and_determinism_and_personality():
    from stock.services.ai_assistant_service import SYSTEM_PROMPT, TOOLS_SYSTEM_PROMPT
    for block in ('SECURITY / TRUST BOUNDARY', 'DETERMINISM & CONSISTENCY',
                  'PERSONALITY & CONDUCT', 'UNTRUSTED CONTENT'):
        assert block in SYSTEM_PROMPT
    # TOOLS_SYSTEM_PROMPT extends SYSTEM_PROMPT, so it inherits all of them.
    assert TOOLS_SYSTEM_PROMPT.startswith(SYSTEM_PROMPT)
