"""Determinism + prompt-caching helpers in base.services.llm."""
from base.services import llm


def test_cache_system_wraps_string():
    assert llm._cache_system('SYS') == [
        {'type': 'text', 'text': 'SYS', 'cache_control': {'type': 'ephemeral'}}]
    assert llm._cache_system('') == ''          # falsy stays falsy
    assert llm._cache_system(None) is None


def test_cache_tools_marks_last_only_without_mutating_input():
    tools = [{'name': 'a'}, {'name': 'b'}]
    out = llm._cache_tools(tools)
    assert 'cache_control' not in out[0]
    assert out[-1]['cache_control'] == {'type': 'ephemeral'}
    assert 'cache_control' not in tools[-1]      # original untouched
    assert llm._cache_tools([]) == []


def test_openai_sampling_kwargs_reasoning_vs_classic():
    reasoning = llm._openai_sampling_kwargs('gpt-5.4-mini')
    assert 'temperature' not in reasoning        # reasoning model rejects temperature
    assert reasoning['seed'] and reasoning['prompt_cache_key']
    assert llm._openai_sampling_kwargs('gpt-4o-mini')['temperature'] == 0
    for m in ('o1-mini', 'o3', 'o4-mini', 'gpt-5-nano'):
        assert 'temperature' not in llm._openai_sampling_kwargs(m)


class _FakeCompletions:
    def __init__(self, reject_extras):
        self.reject_extras = reject_extras
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_extras and any(k in kwargs for k in llm._OPENAI_EXTRAS):
            raise TypeError("unexpected keyword argument 'prompt_cache_key'")
        return 'OK'


def _client(reject_extras=False):
    comp = _FakeCompletions(reject_extras)
    chat = type('Chat', (), {'completions': comp})()
    return type('Client', (), {'chat': chat})(), comp


def test_openai_create_passes_extras_when_supported(monkeypatch):
    monkeypatch.setattr(llm, '_openai_extras_ok', None)
    client, comp = _client(reject_extras=False)
    assert llm._openai_create(client, {'model': 'm', 'seed': 7}) == 'OK'
    assert llm._openai_extras_ok is True
    assert comp.calls[0].get('seed') == 7


def test_openai_create_degrades_when_rejected(monkeypatch):
    monkeypatch.setattr(llm, '_openai_extras_ok', None)
    client, comp = _client(reject_extras=True)
    # First call: extras rejected -> stripped retry succeeds; flag learns False.
    assert llm._openai_create(
        client, {'model': 'm', 'seed': 7, 'prompt_cache_key': 'k'}) == 'OK'
    assert llm._openai_extras_ok is False
    assert len(comp.calls) == 2 and 'seed' not in comp.calls[1]
    # Once learned, later calls skip the extras entirely (single call, no failure).
    client2, comp2 = _client(reject_extras=True)
    assert llm._openai_create(client2, {'model': 'm', 'seed': 7}) == 'OK'
    assert len(comp2.calls) == 1 and 'seed' not in comp2.calls[0]
