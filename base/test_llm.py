"""The AI provider dispatcher (Claude vs Gemini) in base/services/llm.py."""
from base.services import llm


def test_default_provider_is_claude(settings):
    settings.AI_PROVIDER = ''
    assert llm.get_provider() == 'claude'


def test_provider_is_case_insensitive(settings):
    settings.AI_PROVIDER = 'Gemini'
    assert llm.get_provider() == 'gemini'


def test_claude_key_missing(settings):
    settings.AI_PROVIDER = 'claude'
    settings.ANTHROPIC_API_KEY = ''
    text, err = llm.call_ai('hi')
    assert text is None and err == 'llm_key_missing'


def test_gemini_key_missing(settings):
    settings.AI_PROVIDER = 'gemini'
    settings.GEMINI_API_KEY = ''
    text, err = llm.call_ai('hi')
    assert text is None and err == 'llm_key_missing'


def test_dispatch_routes_to_selected_provider(settings, monkeypatch):
    # Confirm call_ai routes by AI_PROVIDER without making a network call.
    monkeypatch.setattr(llm, '_call_gemini', lambda p, s, m: ('GEM', None))
    monkeypatch.setattr(llm, '_call_claude', lambda p, s, m: ('CLAUDE', None))

    settings.AI_PROVIDER = 'gemini'
    assert llm.call_ai('hi')[0] == 'GEM'

    settings.AI_PROVIDER = 'claude'
    assert llm.call_ai('hi')[0] == 'CLAUDE'
