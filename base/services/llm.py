import json
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import openai
except ImportError:
    openai = None

DEFAULT_CLAUDE_MODEL = 'claude-sonnet-4-6'
DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'
DEFAULT_OPENAI_MODEL = 'gpt-5.6-luna'
# GPT-5 reasoning and output share this completion budget.
OPENAI_MIN_COMPLETION_TOKENS = 8192

GEMINI_FALLBACK_MODELS = ('gemini-2.0-flash',)

_TRANSIENT_MARKERS = (
    '503', '529', 'unavailable', 'overloaded', 'high demand',
    '429', 'resource_exhausted', 'rate limit', 'try again',
    'timeout', 'timed out', 'connection error', 'connection reset',
    'connection aborted', 'server disconnected', 'temporarily unavailable',
)

_HARD_MARKERS = (
    'insufficient_quota', 'exceeded your current quota', 'billing',
    'credit balance', 'purchase credits',
    'invalid api key', 'incorrect api key', 'invalid_api_key',
)

_PROVIDER_RATE_LIMIT_MARKERS = (
    '429', '529', 'rate_limit', 'rate limit', 'too many requests',
    'resource_exhausted', 'overloaded', 'high demand',
)

_PROVIDER_CONFIGURATION_MARKERS = _HARD_MARKERS + (
    'authentication_error', 'authentication failed', 'unauthorized',
    'permission_denied', 'permission denied', '401', '403',
)

_PROVIDER_FUNCTIONS = {
    'claude': '_call_claude',
    'gemini': '_call_gemini',
    'openai': '_call_openai',
}


class LLMRequestFailure(str):
    """String-compatible provider failure with safe structured diagnostics.

    Existing callers and tests compare errors as strings, so this deliberately
    subclasses ``str``.  The additional ``failures`` metadata contains only
    provider/model/category/attempt counts; raw SDK messages remain in logs.
    """

    def __new__(cls, message, failures=None, stage='provider'):
        obj = super().__new__(cls, str(message or 'llm_request_failed'))
        obj.failures = list(failures or [])
        obj.stage = stage
        return obj


def _is_transient(err) -> bool:
    e = (err or '').lower()
    if any(m in e for m in _HARD_MARKERS):
        return False
    return any(m in e for m in _TRANSIENT_MARKERS)


def error_category(err) -> str:
    """Classify a provider/tool failure without exposing its raw message."""
    value = str(err or '').lower()
    if value in {'llm_key_missing', 'llm_sdk_missing'}:
        return 'configuration'
    if value == 'llm_provider_invalid':
        return 'configuration'
    if value.startswith('tool_') or value in {
        'invalid_tool_arguments',
        'data_tool_failed',
        'data_scope_required',
    }:
        return 'data_grounding'
    if value == 'openai_empty_response' or 'empty_response' in value:
        return 'empty_response'
    if any(marker in value for marker in _PROVIDER_CONFIGURATION_MARKERS):
        return 'configuration'
    if 'timeout' in value or 'timed out' in value:
        return 'timeout'
    if any(marker in value for marker in (
        'connection error', 'connection reset', 'connection aborted',
        'server disconnected', 'api connection',
    )):
        return 'connection'
    if is_provider_rate_limited(value):
        return 'rate_limit'
    if _is_transient(value):
        return 'temporary_provider_failure'
    if any(marker in value for marker in (
        'bad request', 'invalid request', 'model_not_found',
        'model not found', 'unsupported model',
    )):
        return 'bad_request'
    return 'provider_error'


def is_provider_rate_limited(err) -> bool:
    """Return whether *err* is a provider-side rate/capacity limit.

    Billing, quota-credit and invalid-key failures sometimes include HTTP 429,
    but are configuration problems rather than a temporary rate limit, so hard
    markers take precedence. Raw provider errors stay server-side.
    """
    error = (err or '').lower()
    if any(marker in error for marker in _HARD_MARKERS):
        return False
    return any(marker in error for marker in _PROVIDER_RATE_LIMIT_MARKERS)


def _timeout_seconds():
    """Backward-compatible read timeout value used by provider SDKs."""
    try:
        return max(
            1.0,
            float(getattr(settings, 'LLM_READ_TIMEOUT_SECONDS', 45) or 45),
        )
    except (TypeError, ValueError):
        return 45.0


def _connect_timeout_seconds():
    try:
        return max(
            1.0,
            float(getattr(settings, 'LLM_CONNECT_TIMEOUT_SECONDS', 10) or 10),
        )
    except (TypeError, ValueError):
        return 10.0


def _sdk_timeout():
    """Use distinct connect/read ceilings when httpx is available."""
    try:
        import httpx
        return httpx.Timeout(
            connect=_connect_timeout_seconds(),
            read=_timeout_seconds(),
            write=_timeout_seconds(),
            pool=_connect_timeout_seconds(),
        )
    except (ImportError, TypeError, ValueError):
        return _timeout_seconds()


def _remaining_sdk_timeout(deadline):
    """Cap one provider operation to the request's remaining wall-clock budget."""
    if deadline is None:
        return _sdk_timeout()
    remaining = max(1.0, deadline - time.monotonic())
    try:
        import httpx
        return httpx.Timeout(
            connect=min(_connect_timeout_seconds(), remaining),
            read=min(_timeout_seconds(), remaining),
            write=min(_timeout_seconds(), remaining),
            pool=min(_connect_timeout_seconds(), remaining),
        )
    except (ImportError, TypeError, ValueError):
        return min(_timeout_seconds(), remaining)


def _request_deadline_seconds():
    try:
        return max(
            _timeout_seconds(),
            float(getattr(settings, 'AI_REQUEST_DEADLINE_SECONDS', 110) or 110),
        )
    except (TypeError, ValueError):
        return 110.0


def _provider_model(provider):
    setting_name = {
        'claude': 'ANTHROPIC_MODEL',
        'gemini': 'GEMINI_MODEL',
        'openai': 'OPENAI_MODEL',
    }.get(provider)
    return str(getattr(settings, setting_name, '') or '') if setting_name else ''


def _provider_has_key(provider):
    setting_name = {
        'claude': 'ANTHROPIC_API_KEY',
        'gemini': 'GEMINI_API_KEY',
        'openai': 'OPENAI_API_KEY',
    }.get(provider)
    return bool(setting_name and (getattr(settings, setting_name, '') or ''))


def _provider_order(providers=None):
    if providers is not None:
        values = providers
    else:
        raw = getattr(settings, 'AI_FALLBACK_PROVIDERS', '') or ''
        values = [get_provider(), *str(raw).split(',')]
    order = []
    for value in values:
        provider = str(value or '').strip().lower()
        if provider in _PROVIDER_FUNCTIONS and provider not in order:
            order.append(provider)
    return order


def get_provider():
    return (getattr(settings, 'AI_PROVIDER', '') or 'claude').strip().lower()


def key_missing():
    provider = get_provider()
    if provider == 'gemini':
        return not (getattr(settings, 'GEMINI_API_KEY', '') or '')
    if provider == 'openai':
        return not (getattr(settings, 'OPENAI_API_KEY', '') or '')
    return not (getattr(settings, 'ANTHROPIC_API_KEY', '') or '')


def call_ai(
    prompt,
    system=None,
    max_tokens=2048,
    retries=2,
    history=None,
    *,
    providers=None,
    deadline=None,
):
    """Call the active provider with bounded retry and configured failover.

    Timeouts and connection failures used to fail immediately because their SDK
    strings were not considered transient.  They now receive one retry, while
    fast capacity responses (429/503/529) retain the normal retry budget.  If the
    active provider still cannot answer, another configured provider is tried
    before the request is declared failed.
    """
    deadline = deadline or (time.monotonic() + _request_deadline_seconds())
    failures = []
    last_error = 'llm_request_failed'
    attempted_provider = False

    primary = get_provider()
    provider_order = _provider_order(providers)
    if not provider_order:
        failure = {
            'provider': primary,
            'model': '',
            'category': 'configuration',
            'attempts': 0,
        }
        return None, LLMRequestFailure(
            'llm_provider_invalid',
            failures=[failure],
        )

    for provider in provider_order:
        # Always invoke the selected provider so its established missing-key/SDK
        # contract and test doubles remain intact. Backups with no key are skipped.
        if provider != primary and not _provider_has_key(provider):
            continue
        attempted_provider = True
        fn = globals()[_PROVIDER_FUNCTIONS[provider]]
        delay = 1.0
        provider_attempts = 0
        provider_error = None

        for attempt in range(max(0, int(retries)) + 1):
            if time.monotonic() >= deadline:
                provider_error = 'provider_timeout: request deadline exceeded'
                break
            provider_attempts += 1
            text, provider_error = fn(prompt, system, max_tokens, history)
            if provider_error is None and (text or '').strip():
                if provider != primary:
                    logger.warning(
                        'LLM recovered through fallback provider=%s primary=%s',
                        provider,
                        primary,
                    )
                return text, None
            if provider_error is None:
                provider_error = f'{provider}_empty_response'

            category = error_category(provider_error)
            retryable = _is_transient(provider_error)
            # Network timeouts are expensive; one retry is enough before moving
            # to a healthy backup. Fast 429/503/529 responses may use all retries.
            timeout_budget_spent = (
                category in {'timeout', 'connection'} and attempt >= 1
            )
            if (
                attempt >= max(0, int(retries))
                or not retryable
                or timeout_budget_spent
            ):
                break
            if time.monotonic() + delay >= deadline:
                break
            logger.warning(
                'LLM retry provider=%s attempt=%d/%d category=%s',
                provider,
                attempt + 1,
                retries,
                category,
            )
            time.sleep(delay)
            delay = min(delay * 2, 8)

        last_error = provider_error or last_error
        failures.append({
            'provider': provider,
            'model': _provider_model(provider),
            'category': error_category(last_error),
            'attempts': provider_attempts,
        })

    if not attempted_provider:
        return None, last_error
    return None, LLMRequestFailure(last_error, failures=failures)


def _history_messages(history):
    out = []
    for turn in (history or []):
        try:
            role = turn.get('role')
            content = turn.get('content')
        except AttributeError:
            continue
        if role in ('user', 'assistant') and content:
            out.append({'role': role, 'content': str(content)})
    return out


def _tool_result_failed(value):
    """True when a tool returned a structured error instead of usable evidence."""
    if isinstance(value, dict):
        return bool(value.get('error'))
    if not isinstance(value, str):
        return False
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and bool(parsed.get('error'))


def _tool_result_content(value):
    """Serialize non-string tool results as valid JSON for provider correction."""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def can_use_tools() -> bool:
    provider = get_provider()
    if provider == 'openai':
        return openai is not None
    if provider == 'claude':
        return anthropic is not None
    return False


def call_ai_tools(
    prompt,
    system=None,
    tools=None,
    tool_executor=None,
    max_tokens=4096,
    max_iterations=None,
    retries=2,
    history=None,
    *,
    deadline=None,
    require_tool=True,
):
    deadline = deadline or (time.monotonic() + _request_deadline_seconds())
    if max_iterations is None:
        try:
            max_iterations = int(
                getattr(settings, 'AI_MAX_TOOL_ITERATIONS', 8) or 8
            )
        except (TypeError, ValueError):
            max_iterations = 8
    max_iterations = max(1, min(int(max_iterations), 10))

    if (not can_use_tools() or not tools or tool_executor is None):
        return call_ai(prompt, system=system, max_tokens=max_tokens,
                       retries=retries, history=history, deadline=deadline)

    if get_provider() == 'openai':
        return _openai_tool_loop(prompt, system, tools, tool_executor,
                                 max_tokens, max_iterations, retries, history,
                                 deadline=deadline, require_tool=require_tool)

    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or ''
    if not api_key:
        return None, 'llm_key_missing'
    model = getattr(settings, 'ANTHROPIC_MODEL', '') or DEFAULT_CLAUDE_MODEL

    try:
        client = anthropic.Anthropic(
            api_key=api_key, timeout=_sdk_timeout(), max_retries=0,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception('claude client init failed')
        return None, str(e)

    messages = _history_messages(history) + [{'role': 'user', 'content': prompt}]

    tool_calls_completed = 0
    unresolved_tool_error = False

    def _create(include_tools, *, prevent_tool_use=False):
        kwargs = {'model': model, 'max_tokens': max_tokens, 'messages': messages}
        if system:
            kwargs['system'] = _cache_system(system)
        if include_tools:
            kwargs['tools'] = _cache_tools(tools)
            if prevent_tool_use:
                kwargs['tool_choice'] = {'type': 'none'}
            elif (
                unresolved_tool_error
                or (require_tool and tool_calls_completed == 0)
            ):
                kwargs['tool_choice'] = {'type': 'any'}
        delay = 1.0
        last_err = None
        for attempt in range(retries + 1):
            if time.monotonic() >= deadline:
                return None, 'provider_timeout: request deadline exceeded'
            try:
                return client.messages.create(
                    **kwargs,
                    timeout=_remaining_sdk_timeout(deadline),
                ), None
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                category = error_category(last_err)
                timeout_budget_spent = (
                    category in {'timeout', 'connection'} and attempt >= 1
                )
                if (
                    attempt < retries
                    and _is_transient(last_err)
                    and not timeout_budget_spent
                    and time.monotonic() + delay < deadline
                ):
                    logger.warning('claude transient overload (retry %d/%d): %s',
                                   attempt + 1, retries, last_err[:120])
                    time.sleep(delay)
                    delay = min(delay * 2, 8)
                    continue
                return None, last_err
        return None, last_err

    def _final_text(resp):
        text = ''.join(
            b.text for b in resp.content if getattr(b, 'type', None) == 'text'
        )
        if not text.strip():
            return None, 'claude_empty_response'
        return text, None

    try:
        for _ in range(max_iterations):
            resp, err = _create(include_tools=True)
            if err:
                return None, err
            if getattr(resp, 'stop_reason', None) != 'tool_use':
                if unresolved_tool_error:
                    return None, 'data_tool_failed'
                if require_tool and tool_calls_completed == 0:
                    return None, 'tool_grounding_missing'
                return _final_text(resp)

            messages.append({'role': 'assistant', 'content': resp.content})
            results = []
            unresolved_before_round = unresolved_tool_error
            round_had_error = False
            round_had_success = False
            for block in resp.content:
                if getattr(block, 'type', None) != 'tool_use':
                    continue
                try:
                    out = tool_executor(block.name, dict(block.input or {}))
                    result = {
                        'type': 'tool_result',
                        'tool_use_id': block.id,
                        'content': _tool_result_content(out),
                    }
                    if _tool_result_failed(out):
                        logger.warning(
                            'AI data tool returned an error tool=%s',
                            getattr(block, 'name', '?'),
                        )
                        result['is_error'] = True
                        round_had_error = True
                    else:
                        tool_calls_completed += 1
                        round_had_success = True
                    results.append(result)
                except Exception:  # noqa: BLE001
                    logger.exception('AI tool %s failed', getattr(block, 'name', '?'))
                    return None, 'data_tool_failed'
            messages.append({'role': 'user', 'content': results})
            # A success from the same assistant turn cannot correct an error the
            # provider has not seen yet. Only a clean later tool round resolves it.
            if round_had_error:
                unresolved_tool_error = True
            elif unresolved_before_round and round_had_success:
                unresolved_tool_error = False

        # The tool budget limits data-gathering rounds, not the final prose turn.
        # Synthesize once with tools omitted only when the accumulated evidence
        # is verified and every structured tool error has been resolved.
        if unresolved_tool_error:
            return None, 'data_tool_failed'
        if tool_calls_completed == 0:
            return None, 'tool_iteration_limit'
        # Anthropic requires a tool-result continuation to retain the same tool
        # definitions. Explicit ``none`` keeps that continuation valid while
        # guaranteeing this last request can only synthesize prose.
        resp, err = _create(include_tools=True, prevent_tool_use=True)
        if err:
            return None, err
        return _final_text(resp)
    except Exception as e:  # noqa: BLE001
        logger.exception('claude tool loop failed')
        return None, str(e)


_OPENAI_EXTRAS = ('seed', 'prompt_cache_key', 'temperature')
_openai_extras_ok = None


def _openai_sampling_kwargs(model):
    kw = {
        'seed': int(getattr(settings, 'OPENAI_SEED', 7)),
        'prompt_cache_key': 'alpha-pos-ai-assistant',
    }
    model_name = str(model or '').strip().lower()
    if model_name.startswith('gpt-5.6'):
        effort = str(
            getattr(settings, 'OPENAI_REASONING_EFFORT', 'low') or 'low'
        ).strip().lower()
        if effort not in {'none', 'low', 'medium', 'high', 'xhigh', 'max'}:
            logger.warning(
                'Invalid OPENAI_REASONING_EFFORT=%r; using low',
                effort,
            )
            effort = 'low'
        kw['reasoning_effort'] = effort
    if not model_name.startswith(('gpt-5', 'o1', 'o3', 'o4')):
        kw['temperature'] = float(getattr(settings, 'AI_TEMPERATURE', 0) or 0)
    return kw


def _openai_create(client, kwargs):
    global _openai_extras_ok
    if _openai_extras_ok is False:
        kwargs = {k: v for k, v in kwargs.items() if k not in _OPENAI_EXTRAS}
        return client.chat.completions.create(**kwargs)
    try:
        resp = client.chat.completions.create(**kwargs)
        if _openai_extras_ok is None and any(k in kwargs for k in _OPENAI_EXTRAS):
            _openai_extras_ok = True
        return resp
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        looks_like_param = (
            isinstance(e, TypeError)
            or 'unsupported' in msg or 'unexpected keyword' in msg
            or any(k in msg for k in _OPENAI_EXTRAS)
        )
        if looks_like_param and any(k in kwargs for k in _OPENAI_EXTRAS):
            _openai_extras_ok = False
            logger.warning('openai: determinism/cache kwargs rejected (%s); '
                           'dropping them for this process', str(e)[:120])
            base = {k: v for k, v in kwargs.items() if k not in _OPENAI_EXTRAS}
            return client.chat.completions.create(**base)
        raise


def _cache_system(system):
    if not system:
        return system
    return [{'type': 'text', 'text': system,
             'cache_control': {'type': 'ephemeral'}}]


def _cache_tools(tools):
    if not tools:
        return tools
    cached = [dict(t) for t in tools]
    cached[-1] = {**cached[-1], 'cache_control': {'type': 'ephemeral'}}
    return cached


def _call_claude(prompt, system, max_tokens, history=None):
    if anthropic is None:
        return None, 'llm_sdk_missing'
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or ''
    if not api_key:
        return None, 'llm_key_missing'
    model = getattr(settings, 'ANTHROPIC_MODEL', '') or DEFAULT_CLAUDE_MODEL
    try:
        client = anthropic.Anthropic(
            api_key=api_key, timeout=_sdk_timeout(), max_retries=0,
        )
        kwargs = {
            'model': model,
            'max_tokens': max_tokens,
            'messages': _history_messages(history) + [{'role': 'user', 'content': prompt}],
        }
        if system:
            kwargs['system'] = _cache_system(system)
        resp = client.messages.create(**kwargs)
        text = ''.join(
            b.text for b in resp.content if getattr(b, 'type', None) == 'text'
        )
        return text, None
    except Exception as e:  # noqa: BLE001 — surface a code, log the detail
        logger.exception('claude call failed')
        return None, str(e)


def _call_gemini(prompt, system, max_tokens, history=None):
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, 'llm_sdk_missing'
    api_key = getattr(settings, 'GEMINI_API_KEY', '') or ''
    if not api_key:
        return None, 'llm_key_missing'
    model = getattr(settings, 'GEMINI_MODEL', '') or DEFAULT_GEMINI_MODEL
    convo = ''
    for turn in _history_messages(history):
        who = 'User' if turn['role'] == 'user' else 'Assistant'
        convo += f"{who}: {turn['content']}\n\n"
    contents = ((system + '\n\n') if system else '') + convo + prompt
    try:
        # google-genai takes the request timeout (in milliseconds) via
        # http_options; fall back gracefully if the installed SDK predates it.
        try:
            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=int(_timeout_seconds() * 1000)),
            )
        except (TypeError, AttributeError):
            client = genai.Client(api_key=api_key)
        # Try the configured model, then fall back to a model on a different
        # capacity pool if the primary is overloaded (503). A non-transient error
        # (bad key / bad request) stops immediately — a fallback won't help.
        candidates = [model] + [m for m in GEMINI_FALLBACK_MODELS if m != model]
        last_err = None
        cfg = types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=float(getattr(settings, 'AI_TEMPERATURE', 0) or 0),
        )
        for m in candidates:
            try:
                resp = client.models.generate_content(model=m, contents=contents, config=cfg)
                if m != model:
                    logger.info('gemini: answered via fallback model %s', m)
                return resp.text, None
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                logger.warning('gemini model %s failed: %s', m, last_err[:160])
                if not _is_transient(last_err):
                    return None, last_err
        return None, last_err
    except Exception as e:  # noqa: BLE001 — client construction / unexpected
        logger.exception('gemini call failed')
        return None, str(e)


def _call_openai(prompt, system, max_tokens, history=None):
    if openai is None:
        return None, 'llm_sdk_missing'
    api_key = getattr(settings, 'OPENAI_API_KEY', '') or ''
    if not api_key:
        return None, 'llm_key_missing'
    model = getattr(settings, 'OPENAI_MODEL', '') or DEFAULT_OPENAI_MODEL
    try:
        client = openai.OpenAI(
            api_key=api_key, timeout=_sdk_timeout(), max_retries=0,
        )
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.extend(_history_messages(history))
        messages.append({'role': 'user', 'content': prompt})
        # GPT-5-class models reject the legacy `max_tokens` and bill reasoning +
        # answer against `max_completion_tokens`; keep a generous floor so an
        # answer is never truncated (or eaten entirely by reasoning).
        ceiling = max(int(max_tokens or 2048), OPENAI_MIN_COMPLETION_TOKENS)
        resp = _openai_create(client, {
            'model': model, 'messages': messages, 'max_completion_tokens': ceiling,
            **_openai_sampling_kwargs(model),
        })
        text = resp.choices[0].message.content if resp.choices else None
        if not (text or '').strip():
            # A GPT-5 reasoning model can spend the whole max_completion_tokens
            # budget on reasoning and return empty content (finish_reason='length').
            # Surface that as an error so callers show 'try again' instead of a
            # blank success (and the chat service doesn't store an empty turn).
            return None, 'openai_empty_response'
        return text, None
    except Exception as e:  # noqa: BLE001 — surface a code, log the detail
        logger.exception('openai call failed')
        return None, str(e)


def _openai_tool_loop(prompt, system, tools, tool_executor, max_tokens,
                      max_iterations, retries, history, *, deadline,
                      require_tool=True):
    api_key = getattr(settings, 'OPENAI_API_KEY', '') or ''
    if not api_key:
        return None, 'llm_key_missing'
    model = getattr(settings, 'OPENAI_MODEL', '') or DEFAULT_OPENAI_MODEL
    try:
        client = openai.OpenAI(
            api_key=api_key, timeout=_sdk_timeout(), max_retries=0,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception('openai client init failed')
        return None, str(e)

    oai_tools = [{
        'type': 'function',
        'function': {
            'name': t['name'],
            'description': t.get('description', ''),
            'parameters': t.get('input_schema') or {'type': 'object', 'properties': {}},
        },
    } for t in tools]

    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.extend(_history_messages(history))
    messages.append({'role': 'user', 'content': prompt})
    ceiling = max(int(max_tokens or 2048), OPENAI_MIN_COMPLETION_TOKENS)
    tool_calls_completed = 0
    unresolved_tool_error = False

    def _create(include_tools):
        kwargs = {'model': model, 'messages': messages, 'max_completion_tokens': ceiling,
                  **_openai_sampling_kwargs(model)}
        if include_tools:
            kwargs['tools'] = oai_tools
            if (
                unresolved_tool_error
                or (require_tool and tool_calls_completed == 0)
            ):
                kwargs['tool_choice'] = 'required'
        delay, last_err = 1.0, None
        for attempt in range(retries + 1):
            if time.monotonic() >= deadline:
                return None, 'provider_timeout: request deadline exceeded'
            try:
                kwargs['timeout'] = _remaining_sdk_timeout(deadline)
                return _openai_create(client, kwargs), None
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                category = error_category(last_err)
                timeout_budget_spent = (
                    category in {'timeout', 'connection'} and attempt >= 1
                )
                if (
                    attempt < retries
                    and _is_transient(last_err)
                    and not timeout_budget_spent
                    and time.monotonic() + delay < deadline
                ):
                    logger.warning('openai transient overload (retry %d/%d): %s',
                                   attempt + 1, retries, last_err[:120])
                    time.sleep(delay)
                    delay = min(delay * 2, 8)
                    continue
                return None, last_err
        return None, last_err

    def _final_text(resp):
        msg = resp.choices[0].message if resp.choices else None
        text = (msg.content or '') if msg else ''
        if not text.strip():
            return None, 'openai_empty_response'
        return text, None

    try:
        for _ in range(max_iterations):
            resp, err = _create(include_tools=True)
            if err:
                return None, err
            msg = resp.choices[0].message if resp.choices else None
            tool_calls = getattr(msg, 'tool_calls', None) if msg else None
            if not tool_calls:
                if unresolved_tool_error:
                    return None, 'data_tool_failed'
                if require_tool and tool_calls_completed == 0:
                    return None, 'tool_grounding_missing'
                return _final_text(resp)
            messages.append({
                'role': 'assistant',
                'content': msg.content or '',
                'tool_calls': [{
                    'id': tc.id, 'type': 'function',
                    'function': {'name': tc.function.name,
                                 'arguments': tc.function.arguments or '{}'},
                } for tc in tool_calls],
            })
            unresolved_before_round = unresolved_tool_error
            round_had_error = False
            round_had_success = False
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or '{}')
                except (ValueError, TypeError):
                    logger.warning(
                        'OpenAI returned malformed tool arguments tool=%s',
                        tc.function.name,
                    )
                    return None, 'invalid_tool_arguments'
                if not isinstance(args, dict):
                    return None, 'invalid_tool_arguments'
                try:
                    out = tool_executor(tc.function.name, args)
                    if _tool_result_failed(out):
                        logger.warning(
                            'AI data tool returned an error tool=%s',
                            tc.function.name,
                        )
                        round_had_error = True
                    else:
                        tool_calls_completed += 1
                        round_had_success = True
                except Exception:  # noqa: BLE001
                    logger.exception('AI tool %s failed', tc.function.name)
                    return None, 'data_tool_failed'
                messages.append({
                    'role': 'tool', 'tool_call_id': tc.id,
                    'content': _tool_result_content(out),
                })
            # A success from the same assistant turn cannot correct an error the
            # provider has not seen yet. Only a clean later tool round resolves it.
            if round_had_error:
                unresolved_tool_error = True
            elif unresolved_before_round and round_had_success:
                unresolved_tool_error = False

        # The tool budget limits data-gathering rounds, not the final prose turn.
        # Synthesize once with tools omitted only when the accumulated evidence
        # is verified and every structured tool error has been resolved.
        if unresolved_tool_error:
            return None, 'data_tool_failed'
        if tool_calls_completed == 0:
            return None, 'tool_iteration_limit'
        resp, err = _create(include_tools=False)
        if err:
            return None, err
        return _final_text(resp)
    except Exception as e:  # noqa: BLE001
        logger.exception('openai tool loop failed')
        return None, str(e)
