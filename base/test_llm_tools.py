"""call_ai_tools: the Claude tool-use loop that lets the assistant read the live
database in detail, plus its fallback to a single call for non-Claude providers."""
import types

from base.services import llm


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _Msgs:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _fake_anthropic(msgs):
    """A stand-in for the `anthropic` module: .Anthropic(...) -> client.messages."""
    client = types.SimpleNamespace(messages=msgs)
    return types.SimpleNamespace(Anthropic=lambda **kw: client)


def test_can_use_tools_only_for_claude(settings):
    settings.AI_PROVIDER = 'claude'
    assert llm.can_use_tools() is True   # anthropic SDK is installed in the venv
    settings.AI_PROVIDER = 'gemini'
    assert llm.can_use_tools() is False


def test_call_ai_tools_falls_back_when_not_claude(settings, monkeypatch):
    settings.AI_PROVIDER = 'gemini'
    seen = {}

    def fake_call_ai(prompt, system=None, max_tokens=2048, retries=2, history=None):
        seen['prompt'] = prompt
        return 'PLAIN', None

    monkeypatch.setattr(llm, 'call_ai', fake_call_ai)
    text, err = llm.call_ai_tools(
        'hi', tools=[{'name': 'x'}], tool_executor=lambda n, a: '{}')
    assert text == 'PLAIN' and err is None and seen['prompt'] == 'hi'


def test_call_ai_tools_falls_back_when_no_tools(settings, monkeypatch):
    settings.AI_PROVIDER = 'claude'
    monkeypatch.setattr(llm, 'call_ai', lambda *a, **k: ('PLAIN', None))
    text, err = llm.call_ai_tools('hi', tools=None, tool_executor=None)
    assert text == 'PLAIN' and err is None


def test_call_ai_tools_runs_the_tool_loop(settings, monkeypatch):
    settings.AI_PROVIDER = 'claude'
    settings.ANTHROPIC_API_KEY = 'k'

    tool_use = _Block(type='tool_use', id='tu1', name='get_overview', input={})
    r1 = _Resp([tool_use], 'tool_use')
    r2 = _Resp([_Block(type='text', text='FINAL ANSWER')], 'end_turn')
    msgs = _Msgs([r1, r2])
    monkeypatch.setattr(llm, 'anthropic', _fake_anthropic(msgs))

    ran = {}

    def executor(name, inp):
        ran['name'] = name
        ran['input'] = inp
        return '{"ok": true}'

    text, err = llm.call_ai_tools(
        'q', system='sys', tools=[{'name': 'get_overview'}], tool_executor=executor)

    assert err is None and text == 'FINAL ANSWER'
    assert ran['name'] == 'get_overview' and ran['input'] == {}
    # Two create() calls: the tool round, then the answer round, and the second
    # must carry the tool_result back to the model.
    assert len(msgs.calls) == 2
    second = msgs.calls[1]['messages']
    assert any(
        m['role'] == 'user' and isinstance(m['content'], list)
        and m['content'][0].get('type') == 'tool_result'
        for m in second
    )


def test_history_messages_filters_to_clean_turns():
    out = llm._history_messages([
        {'role': 'user', 'content': 'a'},
        {'role': 'assistant', 'content': 'b'},
        {'role': 'system', 'content': 'dropped'},   # only user/assistant kept
        {'role': 'user', 'content': ''},             # empty dropped
        'garbage',                                   # non-dict dropped
        {'role': 'user', 'content': 'c'},
    ])
    assert out == [
        {'role': 'user', 'content': 'a'},
        {'role': 'assistant', 'content': 'b'},
        {'role': 'user', 'content': 'c'},
    ]


def test_call_openai_builds_messages_and_uses_completion_tokens(settings, monkeypatch):
    settings.AI_PROVIDER = 'openai'
    settings.OPENAI_API_KEY = 'k'
    settings.OPENAI_MODEL = 'gpt-5.4-mini'

    captured = {}

    class _Msg:
        content = 'OPENAI ANSWER'

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kw):
            captured.update(kw)
            return _Resp()

    class _Client:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(llm, 'openai', types.SimpleNamespace(OpenAI=lambda **kw: _Client(**kw)))

    text, err = llm.call_ai(
        'best cashier?', system='SYS',
        history=[{'role': 'user', 'content': 'q1'}, {'role': 'assistant', 'content': 'a1'}])

    assert err is None and text == 'OPENAI ANSWER'
    msgs = captured['messages']
    assert msgs[0] == {'role': 'system', 'content': 'SYS'}
    assert msgs[1] == {'role': 'user', 'content': 'q1'}
    assert msgs[2] == {'role': 'assistant', 'content': 'a1'}
    assert msgs[3] == {'role': 'user', 'content': 'best cashier?'}
    # GPT-5-class models reject the legacy max_tokens.
    assert 'max_completion_tokens' in captured and 'max_tokens' not in captured
    assert captured['model'] == 'gpt-5.4-mini'


def test_call_openai_empty_response_is_an_error(settings, monkeypatch):
    # A GPT-5 reasoning model can return empty content (finish_reason='length');
    # that must surface as an error, not a blank success.
    settings.AI_PROVIDER = 'openai'
    settings.OPENAI_API_KEY = 'k'

    class _Msg:
        content = None

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Client:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=lambda **k: _Resp()))

    monkeypatch.setattr(llm, 'openai', types.SimpleNamespace(OpenAI=lambda **kw: _Client(**kw)))
    text, err = llm.call_ai('hi')
    assert text is None and err == 'openai_empty_response'


def test_call_ai_tools_surfaces_tool_errors_without_crashing(settings, monkeypatch):
    settings.AI_PROVIDER = 'claude'
    settings.ANTHROPIC_API_KEY = 'k'

    tool_use = _Block(type='tool_use', id='tu1', name='boom', input={})
    r1 = _Resp([tool_use], 'tool_use')
    r2 = _Resp([_Block(type='text', text='handled')], 'end_turn')
    msgs = _Msgs([r1, r2])
    monkeypatch.setattr(llm, 'anthropic', _fake_anthropic(msgs))

    def executor(name, inp):
        raise RuntimeError('kaboom')

    text, err = llm.call_ai_tools(
        'q', tools=[{'name': 'boom'}], tool_executor=executor)
    assert err is None and text == 'handled'
    # The error is reported back to the model as an is_error tool_result.
    second = msgs.calls[1]['messages']
    tr = next(m['content'][0] for m in second
              if m['role'] == 'user' and isinstance(m['content'], list))
    assert tr.get('is_error') is True and 'kaboom' in tr['content']
