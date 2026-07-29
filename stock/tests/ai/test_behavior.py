import pytest

from stock.services.ai_assistant_service import AIStockAssistant as A


def test_system_prompt_requires_warm_patient_answers():
    from stock.services.ai_assistant_service import SYSTEM_PROMPT, TOOLS_SYSTEM_PROMPT

    assert 'Always sound warm, patient' in SYSTEM_PROMPT
    assert 'Treat repeated questions as normal requests' in SYSTEM_PROMPT
    assert 'answer every part clearly and in the order asked' in SYSTEM_PROMPT
    for forbidden in (
        'Am I being tested again',
        'mildly-annoyed',
        'BEHAVIOR:',
    ):
        assert forbidden not in SYSTEM_PROMPT
    assert TOOLS_SYSTEM_PROMPT.startswith(SYSTEM_PROMPT)


@pytest.mark.django_db
def test_repeated_multi_question_has_no_behavior_directive(monkeypatch):
    import base.services.llm as llm

    captured = {}
    history = [
        {'role': 'user', 'content': 'sales today and payment mix'},
        {'role': 'assistant', 'content': 'Earlier answer'},
    ]
    query = 'sales today and payment mix'

    monkeypatch.setattr(llm, 'can_use_tools', lambda: False)
    monkeypatch.setattr(A, '_get_all_stock_data', lambda: {})
    monkeypatch.setattr(A, '_get_sales_data', lambda: {})
    monkeypatch.setattr(A, '_needs_analytics', lambda value: False)

    def fake_call_ai(prompt, system=None, max_tokens=None, history=None, **kwargs):
        captured['prompt'] = prompt
        captured['system'] = system
        captured['history'] = history
        return 'Warm complete answer', None

    monkeypatch.setattr(llm, 'call_ai', fake_call_ai)

    result = A.process_query(query, history=history)

    assert result['success'] is True
    assert query in captured['prompt']
    assert captured['history'] == history
    assert 'BEHAVIOR:' not in captured['prompt']
    assert 'tested again' not in captured['prompt'].lower()
    assert 'Always sound warm, patient' in captured['system']


def test_system_prompt_keeps_security_and_determinism_contracts():
    from stock.services.ai_assistant_service import SYSTEM_PROMPT

    for block in (
        'SECURITY / TRUST BOUNDARY',
        'ACCURACY & DATA GROUNDING',
        'DETERMINISM & CONSISTENCY',
        'PERSONALITY & CONDUCT',
        'UNTRUSTED CONTENT',
    ):
        assert block in SYSTEM_PROMPT
