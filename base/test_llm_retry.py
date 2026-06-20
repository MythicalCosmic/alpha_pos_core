"""call_ai must survive a transient provider overload (Gemini flash 503 'high
demand') by retrying + falling back to another model, instead of failing hard."""
import sys
import types as _types

import pytest

from base.services import llm


def test_is_transient_classification():
    assert llm._is_transient("503 UNAVAILABLE. high demand")
    assert llm._is_transient("429 RESOURCE_EXHAUSTED")
    assert llm._is_transient("the model is overloaded")
    assert not llm._is_transient("401 invalid api key")
    assert not llm._is_transient("400 invalid model name")
    assert not llm._is_transient("")
    # 429 insufficient_quota is a billing issue, not a transient overload — fail fast.
    assert not llm._is_transient("Error code: 429 - insufficient_quota: You exceeded your current quota")


@pytest.mark.django_db
def test_call_ai_retries_transient_then_succeeds(settings, monkeypatch):
    settings.AI_PROVIDER = 'gemini'
    monkeypatch.setattr(llm.time, 'sleep', lambda *_: None)
    n = {'c': 0}

    def fake(prompt, system, max_tokens, history=None):
        n['c'] += 1
        return ("OK", None) if n['c'] >= 2 else (None, "503 UNAVAILABLE high demand")

    monkeypatch.setattr(llm, '_call_gemini', fake)
    text, err = llm.call_ai("hi")
    assert text == "OK" and err is None and n['c'] == 2


@pytest.mark.django_db
def test_call_ai_does_not_retry_auth_error(settings, monkeypatch):
    settings.AI_PROVIDER = 'gemini'
    n = {'c': 0}

    def fake(prompt, system, max_tokens, history=None):
        n['c'] += 1
        return None, "401 API key not valid"

    monkeypatch.setattr(llm, '_call_gemini', fake)
    text, err = llm.call_ai("hi")
    assert err and 'not valid' in err and n['c'] == 1   # not retried


@pytest.mark.django_db
def test_gemini_falls_back_to_second_model(settings, monkeypatch):
    settings.AI_PROVIDER = 'gemini'
    settings.GEMINI_API_KEY = 'k'
    settings.GEMINI_MODEL = 'gemini-2.5-flash'
    used = []

    class _Resp:
        text = "ANSWER"

    class _Models:
        def generate_content(self, model, contents, config):
            used.append(model)
            if model == 'gemini-2.5-flash':
                raise RuntimeError("503 UNAVAILABLE high demand")
            return _Resp()

    class _Client:
        def __init__(self, *a, **k):
            self.models = _Models()

    # Inject a fake google.genai (the SDK is server-only; not in this local venv).
    types_mod = _types.ModuleType('google.genai.types')
    types_mod.GenerateContentConfig = lambda **k: object()
    types_mod.HttpOptions = lambda **k: object()
    genai_mod = _types.ModuleType('google.genai')
    genai_mod.Client = _Client
    genai_mod.types = types_mod
    genai_mod.__path__ = []
    google_mod = _types.ModuleType('google')
    google_mod.genai = genai_mod
    google_mod.__path__ = []
    monkeypatch.setitem(sys.modules, 'google', google_mod)
    monkeypatch.setitem(sys.modules, 'google.genai', genai_mod)
    monkeypatch.setitem(sys.modules, 'google.genai.types', types_mod)

    text, err = llm._call_gemini("hi", None, 256)
    assert err is None and text == "ANSWER"
    assert used == ['gemini-2.5-flash', 'gemini-2.0-flash']   # primary -> fallback
