"""call_ai_tools: the Claude tool-use loop that lets the assistant read the live
database in detail, plus its fallback to a single call for non-Claude providers."""
import json
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


def _openai_tool_call(call_id, name, args):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(
            name=name,
            arguments=json.dumps(args),
        ),
    )


def _openai_response(content=None, tool_calls=None):
    message = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message)],
    )


def test_can_use_tools_only_for_claude(settings):
    settings.AI_PROVIDER = 'claude'
    assert llm.can_use_tools() is True   # anthropic SDK is installed in the venv
    settings.AI_PROVIDER = 'gemini'
    assert llm.can_use_tools() is False


def test_call_ai_tools_falls_back_when_not_claude(settings, monkeypatch):
    settings.AI_PROVIDER = 'gemini'
    seen = {}

    def fake_call_ai(
        prompt,
        system=None,
        max_tokens=2048,
        retries=2,
        history=None,
        **kwargs,
    ):
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


def test_can_use_tools_for_openai(settings):
    settings.AI_PROVIDER = 'openai'
    assert llm.can_use_tools() is True   # openai SDK is installed in the venv


def test_openai_tool_loop_runs_function_calls(settings, monkeypatch):
    settings.AI_PROVIDER = 'openai'
    settings.OPENAI_API_KEY = 'k'

    class _Func:
        def __init__(self, name, args):
            self.name = name
            self.arguments = args

    class _TC:
        def __init__(self, id, name, args):
            self.id = id
            self.function = _Func(name, args)

    class _Msg:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Resp:
        def __init__(self, msg):
            self.choices = [types.SimpleNamespace(message=msg)]

    responses = [
        _Resp(_Msg(tool_calls=[_TC('call_1', 'list_orders', '{"date": "2026-06-19"}')])),
        _Resp(_Msg(content='FINAL ANSWER')),
    ]
    calls = []

    class _Completions:
        def create(self, **kw):
            calls.append(kw)
            return responses.pop(0)

    class _Client:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(llm, 'openai', types.SimpleNamespace(OpenAI=lambda **kw: _Client(**kw)))

    ran = {}

    def executor(name, args):
        ran['name'] = name
        ran['args'] = args
        return '{"orders": 3}'

    text, err = llm.call_ai_tools(
        'how many orders on 2026-06-19?',
        system='SYS',
        tools=[{'name': 'list_orders', 'description': 'list orders',
                'input_schema': {'type': 'object', 'properties': {'date': {'type': 'string'}}}}],
        tool_executor=executor,
    )
    assert err is None and text == 'FINAL ANSWER'
    assert ran['name'] == 'list_orders' and ran['args'] == {'date': '2026-06-19'}
    # First create() sent the tools as OpenAI functions and uses max_completion_tokens.
    assert calls[0]['tools'][0]['type'] == 'function'
    assert calls[0]['tools'][0]['function']['name'] == 'list_orders'
    assert 'max_completion_tokens' in calls[0] and 'max_tokens' not in calls[0]
    # Second create() carried the tool result back as a role:'tool' message.
    assert any(m.get('role') == 'tool' and m.get('content') == '{"orders": 3}'
               for m in calls[1]['messages'])

    # A failing data tool must fail closed. Continuing without the evidence can
    # produce a plausible but false money/stock answer.
    responses.extend([
        _Resp(_Msg(tool_calls=[_TC('call_2', 'list_orders', '{}')])),
        _Resp(_Msg(content='SAFE FINAL ANSWER')),
    ])

    def failing_executor(name, args):
        raise RuntimeError('secret database table name')

    text, err = llm.call_ai_tools(
        'try again', tools=[{'name': 'list_orders'}],
        tool_executor=failing_executor,
    )
    assert text is None and err == 'data_tool_failed'
    assert 'secret database table name' not in str(err)


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
    assert text is None and err == 'data_tool_failed'
    assert len(msgs.calls) == 1
    assert 'kaboom' not in str(err)


def test_openai_corrects_live_shaped_query_db_field_error(
        settings,
        monkeypatch,
):
    settings.AI_PROVIDER = 'openai'
    settings.OPENAI_API_KEY = 'k'

    successful_calls = [
        _openai_tool_call(
            f'call_{index}',
            'query_db',
            {'model': 'order', 'fields': ['id'], 'offset': index},
        )
        for index in range(11)
    ]
    invalid_call = _openai_tool_call(
        'call_invalid',
        'query_db',
        {'model': 'orderitem', 'fields': ['total_price']},
    )
    corrected_call = _openai_tool_call(
        'call_corrected',
        'query_db',
        {'model': 'orderitem', 'fields': ['line_total_uzs']},
    )
    responses = [
        _openai_response(tool_calls=[*successful_calls, invalid_call]),
        _openai_response(tool_calls=[corrected_call]),
        _openai_response(content='CORRECTED ANSWER'),
    ]
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=_Completions(),
    ))
    monkeypatch.setattr(
        llm,
        'openai',
        types.SimpleNamespace(OpenAI=lambda **kwargs: client),
    )

    executed = []

    def executor(name, args):
        executed.append((name, args))
        if args.get('fields') == ['total_price']:
            return {
                'error': (
                    "bad fields/order_by (Cannot resolve keyword "
                    "'total_price' into field.)"
                ),
            }
        return {'rows': [{'line_total_uzs': 200}]}

    text, err = llm.call_ai_tools(
        'Analyse the order-item totals.',
        tools=[{'name': 'query_db'}],
        tool_executor=executor,
        max_iterations=8,
    )

    assert err is None and text == 'CORRECTED ANSWER'
    assert len(executed) == 13
    # Eleven good calls do not clear an error in that same assistant turn. The
    # next request is forced to use a tool so the model must correct the query.
    assert calls[1]['tool_choice'] == 'required'
    assert 'tool_choice' not in calls[2]
    error_result = next(
        message for message in calls[1]['messages']
        if message.get('role') == 'tool'
        and message.get('tool_call_id') == 'call_invalid'
    )
    assert json.loads(error_result['content'])['error'].startswith(
        'bad fields/order_by'
    )


def test_claude_corrects_live_shaped_query_db_field_error(
        settings,
        monkeypatch,
):
    settings.AI_PROVIDER = 'claude'
    settings.ANTHROPIC_API_KEY = 'k'

    successful_calls = [
        _Block(
            type='tool_use',
            id=f'tu_{index}',
            name='query_db',
            input={'model': 'order', 'fields': ['id'], 'offset': index},
        )
        for index in range(11)
    ]
    invalid_call = _Block(
        type='tool_use',
        id='tu_invalid',
        name='query_db',
        input={'model': 'orderitem', 'fields': ['total_price']},
    )
    corrected_call = _Block(
        type='tool_use',
        id='tu_corrected',
        name='query_db',
        input={'model': 'orderitem', 'fields': ['line_total_uzs']},
    )
    msgs = _Msgs([
        _Resp([*successful_calls, invalid_call], 'tool_use'),
        _Resp([corrected_call], 'tool_use'),
        _Resp([_Block(type='text', text='CORRECTED ANSWER')], 'end_turn'),
    ])
    monkeypatch.setattr(llm, 'anthropic', _fake_anthropic(msgs))

    executed = []

    def executor(name, args):
        executed.append((name, args))
        if args.get('fields') == ['total_price']:
            return {
                'error': (
                    "bad fields/order_by (Cannot resolve keyword "
                    "'total_price' into field.)"
                ),
            }
        return {'rows': [{'line_total_uzs': 200}]}

    text, err = llm.call_ai_tools(
        'Analyse the order-item totals.',
        tools=[{'name': 'query_db'}],
        tool_executor=executor,
        max_iterations=8,
    )

    assert err is None and text == 'CORRECTED ANSWER'
    assert len(executed) == 13
    assert msgs.calls[1]['tool_choice'] == {'type': 'any'}
    assert 'tool_choice' not in msgs.calls[2]
    error_result = next(
        result
        for message in msgs.calls[1]['messages']
        if message.get('role') == 'user'
        and isinstance(message.get('content'), list)
        for result in message['content']
        if result.get('tool_use_id') == 'tu_invalid'
    )
    assert error_result['is_error'] is True
    assert json.loads(error_result['content'])['error'].startswith(
        'bad fields/order_by'
    )


def test_openai_unresolved_structured_tool_error_fails_closed(
        settings,
        monkeypatch,
):
    settings.AI_PROVIDER = 'openai'
    settings.OPENAI_API_KEY = 'k'
    responses = [
        _openai_response(tool_calls=[
            _openai_tool_call(
                'call_invalid',
                'query_db',
                {'model': 'orderitem', 'fields': ['total_price']},
            ),
        ]),
        _openai_response(content='UNGROUNDED ANSWER'),
    ]
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=_Completions(),
    ))
    monkeypatch.setattr(
        llm,
        'openai',
        types.SimpleNamespace(OpenAI=lambda **kwargs: client),
    )

    text, err = llm.call_ai_tools(
        'q',
        tools=[{'name': 'query_db'}],
        tool_executor=lambda *args: {'error': 'invalid field'},
    )

    assert text is None and err == 'data_tool_failed'
    assert len(calls) == 2
    assert calls[1]['tool_choice'] == 'required'


def test_claude_unresolved_structured_tool_error_fails_closed(
        settings,
        monkeypatch,
):
    settings.AI_PROVIDER = 'claude'
    settings.ANTHROPIC_API_KEY = 'k'
    invalid_call = _Block(
        type='tool_use',
        id='tu_invalid',
        name='query_db',
        input={'model': 'orderitem', 'fields': ['total_price']},
    )
    msgs = _Msgs([
        _Resp([invalid_call], 'tool_use'),
        _Resp([_Block(type='text', text='UNGROUNDED ANSWER')], 'end_turn'),
    ])
    monkeypatch.setattr(llm, 'anthropic', _fake_anthropic(msgs))

    text, err = llm.call_ai_tools(
        'q',
        tools=[{'name': 'query_db'}],
        tool_executor=lambda *args: {'error': 'invalid field'},
    )

    assert text is None and err == 'data_tool_failed'
    assert len(msgs.calls) == 2
    assert msgs.calls[1]['tool_choice'] == {'type': 'any'}


def test_openai_malformed_tool_arguments_fail_closed(settings, monkeypatch):
    settings.AI_PROVIDER = 'openai'
    settings.OPENAI_API_KEY = 'k'

    tool_call = types.SimpleNamespace(
        id='call_bad',
        function=types.SimpleNamespace(
            name='list_orders',
            arguments='{not-json',
        ),
    )
    response = types.SimpleNamespace(choices=[
        types.SimpleNamespace(message=types.SimpleNamespace(
            content=None,
            tool_calls=[tool_call],
        )),
    ])
    client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=lambda **kwargs: response),
    ))
    monkeypatch.setattr(
        llm,
        'openai',
        types.SimpleNamespace(OpenAI=lambda **kwargs: client),
    )
    called = []

    text, err = llm.call_ai_tools(
        'orders?',
        tools=[{'name': 'list_orders'}],
        tool_executor=lambda *args: called.append(args),
    )

    assert text is None and err == 'invalid_tool_arguments'
    assert called == []


def test_claude_clean_tool_iteration_limit_synthesizes_final_answer(
        settings,
        monkeypatch,
):
    settings.AI_PROVIDER = 'claude'
    settings.ANTHROPIC_API_KEY = 'k'
    tool_use = _Block(type='tool_use', id='tu1', name='get_overview', input={})
    msgs = _Msgs([
        _Resp([tool_use], 'tool_use'),
        _Resp([_Block(type='text', text='SYNTHESIZED ANSWER')], 'end_turn'),
    ])
    monkeypatch.setattr(llm, 'anthropic', _fake_anthropic(msgs))

    text, err = llm.call_ai_tools(
        'q',
        tools=[{'name': 'get_overview'}],
        tool_executor=lambda *args: '{"ok": true}',
        max_iterations=1,
    )

    assert err is None and text == 'SYNTHESIZED ANSWER'
    assert len(msgs.calls) == 2
    assert 'tools' in msgs.calls[0]
    assert 'tools' in msgs.calls[1]
    assert msgs.calls[1]['tool_choice'] == {'type': 'none'}
    assert 'timeout' in msgs.calls[1]
    assert any(
        message.get('role') == 'user'
        and isinstance(message.get('content'), list)
        and message['content'][0].get('type') == 'tool_result'
        for message in msgs.calls[1]['messages']
    )


def test_openai_clean_tool_iteration_limit_synthesizes_final_answer(
        settings,
        monkeypatch,
):
    settings.AI_PROVIDER = 'openai'
    settings.OPENAI_API_KEY = 'k'
    responses = [
        _openai_response(tool_calls=[
            _openai_tool_call('call_1', 'get_overview', {}),
        ]),
        _openai_response(content='SYNTHESIZED ANSWER'),
    ]
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=_Completions(),
    ))
    monkeypatch.setattr(
        llm,
        'openai',
        types.SimpleNamespace(OpenAI=lambda **kwargs: client),
    )

    text, err = llm.call_ai_tools(
        'q',
        tools=[{'name': 'get_overview'}],
        tool_executor=lambda *args: '{"ok": true}',
        max_iterations=1,
    )

    assert err is None and text == 'SYNTHESIZED ANSWER'
    assert len(calls) == 2
    assert 'tools' in calls[0]
    assert 'tools' not in calls[1]
    assert 'tool_choice' not in calls[1]
    assert 'timeout' in calls[1]
    assert any(
        message.get('role') == 'tool'
        and message.get('content') == '{"ok": true}'
        for message in calls[1]['messages']
    )


def test_claude_unresolved_tool_error_rejects_budget_synthesis(
        settings,
        monkeypatch,
):
    settings.AI_PROVIDER = 'claude'
    settings.ANTHROPIC_API_KEY = 'k'
    tool_use = _Block(type='tool_use', id='tu1', name='query_db', input={})
    msgs = _Msgs([_Resp([tool_use], 'tool_use')])
    monkeypatch.setattr(llm, 'anthropic', _fake_anthropic(msgs))

    text, err = llm.call_ai_tools(
        'q',
        tools=[{'name': 'query_db'}],
        tool_executor=lambda *args: '{"error": "invalid field"}',
        max_iterations=1,
    )

    assert text is None and err == 'data_tool_failed'
    assert len(msgs.calls) == 1


def test_openai_unresolved_tool_error_rejects_budget_synthesis(
        settings,
        monkeypatch,
):
    settings.AI_PROVIDER = 'openai'
    settings.OPENAI_API_KEY = 'k'
    responses = [
        _openai_response(tool_calls=[
            _openai_tool_call('call_1', 'query_db', {}),
        ]),
    ]
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=_Completions(),
    ))
    monkeypatch.setattr(
        llm,
        'openai',
        types.SimpleNamespace(OpenAI=lambda **kwargs: client),
    )

    text, err = llm.call_ai_tools(
        'q',
        tools=[{'name': 'query_db'}],
        tool_executor=lambda *args: '{"error": "invalid field"}',
        max_iterations=1,
    )

    assert text is None and err == 'data_tool_failed'
    assert len(calls) == 1


def test_openai_tool_timeout_retries_only_once(settings, monkeypatch):
    settings.AI_PROVIDER = 'openai'
    settings.OPENAI_API_KEY = 'k'
    monkeypatch.setattr(llm.time, 'sleep', lambda *_: None)
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError('openai.APITimeoutError: Request timed out')

    client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=_Completions(),
    ))
    monkeypatch.setattr(
        llm,
        'openai',
        types.SimpleNamespace(OpenAI=lambda **kwargs: client),
    )

    text, err = llm.call_ai_tools(
        'orders?',
        tools=[{'name': 'list_orders'}],
        tool_executor=lambda *args: '{"orders": []}',
        retries=2,
    )

    assert text is None
    assert 'timed out' in str(err)
    assert len(calls) == 2


def test_claude_tool_loop_rejects_blank_final_answer(settings, monkeypatch):
    settings.AI_PROVIDER = 'claude'
    settings.ANTHROPIC_API_KEY = 'k'
    tool_use = _Block(type='tool_use', id='tu1', name='get_overview', input={})
    msgs = _Msgs([
        _Resp([tool_use], 'tool_use'),
        _Resp([_Block(type='text', text='   ')], 'end_turn'),
    ])
    monkeypatch.setattr(llm, 'anthropic', _fake_anthropic(msgs))

    text, err = llm.call_ai_tools(
        'q',
        tools=[{'name': 'get_overview'}],
        tool_executor=lambda *args: '{"ok": true}',
    )

    assert text is None
    assert err == 'claude_empty_response'
