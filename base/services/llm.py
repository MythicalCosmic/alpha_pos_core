"""Single entry point for LLM calls — Claude (Anthropic), Gemini (Google) or
OpenAI.

Both the stock AI assistant and the demand forecaster call `call_ai()`, which
dispatches to whichever provider the operator selected. Everything is
operator-configured (desktop panel / env):

    AI_PROVIDER        — 'claude' (default), 'gemini', or 'openai'.
    ANTHROPIC_API_KEY  — required when AI_PROVIDER=claude.
    ANTHROPIC_MODEL    — defaults to claude-sonnet-4-6 (also: claude-sonnet-4-5,
                         claude-opus-4-8).
    GEMINI_API_KEY     — required when AI_PROVIDER=gemini.
    GEMINI_MODEL       — defaults to gemini-2.5-flash.
    OPENAI_API_KEY     — required when AI_PROVIDER=openai.
    OPENAI_MODEL       — defaults to gpt-5.4-mini.

`call_ai()` / `call_ai_tools()` accept an optional `history` (a list of
{'role': 'user'|'assistant', 'content': str} prior turns) so the assistant can
hold a multi-message conversation; the providers fold it in natively.

Both backends return (text, error) where error is None on success, or one of
'llm_sdk_missing' / 'llm_key_missing' / a raw error string. The callers handle
those codes identically regardless of provider, so switching is a config change.
"""
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

# Current Sonnet — same price as 4.5, 1M context. Override via ANTHROPIC_MODEL.
DEFAULT_CLAUDE_MODEL = 'claude-sonnet-4-6'
DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'
DEFAULT_OPENAI_MODEL = 'gpt-5.4-mini'
# GPT-5-class models bill reasoning + answer against one ceiling and use
# `max_completion_tokens` (not the legacy `max_tokens`); keep a generous floor so
# a long answer (or any internal reasoning) is never truncated mid-sentence.
OPENAI_MIN_COMPLETION_TOKENS = 4096

# If the configured Gemini model is overloaded (503 'high demand'), fall back to a
# model on a different capacity pool before giving up — the flash models spike
# independently. Tried in order after the configured one.
GEMINI_FALLBACK_MODELS = ('gemini-2.0-flash',)

# Provider-side overloads worth retrying rather than surfacing as a hard failure:
# Gemini flash 503 UNAVAILABLE 'high demand', 429 quota spikes, Anthropic 529
# overloaded. Matched (case-insensitively) against the SDK's error string.
_TRANSIENT_MARKERS = (
    '503', '529', 'unavailable', 'overloaded', 'high demand',
    '429', 'resource_exhausted', 'rate limit', 'try again',
)

# Errors that *look* transient (often carry a 429) but won't recover by retrying:
# an account with no credits/billing, or a bad key. Fail fast instead of burning
# the backoff budget — the operator must fix billing / the key.
_HARD_MARKERS = (
    'insufficient_quota', 'exceeded your current quota', 'billing',
    'invalid api key', 'incorrect api key', 'invalid_api_key',
)


def _is_transient(err) -> bool:
    e = (err or '').lower()
    if any(m in e for m in _HARD_MARKERS):
        return False
    return any(m in e for m in _TRANSIENT_MARKERS)


def _timeout_seconds():
    # call_ai runs synchronously in the request/worker thread. Without an
    # explicit timeout the Anthropic SDK waits up to 600s, pinning a worker on a
    # hung provider. Cap it (override via LLM_TIMEOUT_SECONDS).
    try:
        return float(getattr(settings, 'LLM_TIMEOUT_SECONDS', 30) or 30)
    except (TypeError, ValueError):
        return 30.0


def get_provider():
    return (getattr(settings, 'AI_PROVIDER', '') or 'claude').strip().lower()


def key_missing():
    """True when the *active* provider's API key is not configured. Lets callers
    fail fast with a clear message instead of gating on one provider's key
    (the view used to check GEMINI_API_KEY even when the default provider is
    Claude, so a Claude-configured deployment was wrongly reported unconfigured)."""
    provider = get_provider()
    if provider == 'gemini':
        return not (getattr(settings, 'GEMINI_API_KEY', '') or '')
    if provider == 'openai':
        return not (getattr(settings, 'OPENAI_API_KEY', '') or '')
    return not (getattr(settings, 'ANTHROPIC_API_KEY', '') or '')


def call_ai(prompt, system=None, max_tokens=2048, retries=2, history=None):
    """Dispatch to the configured provider, retrying transient provider overloads
    (Gemini flash 503 'high demand' / 429, Anthropic 529, OpenAI 429) with backoff.
    A 503 returns fast, so the retries cost little and turn a transient spike into a
    successful answer instead of 'AI assistant temporarily unavailable'. `history`
    is an optional list of prior {'role','content'} turns for multi-turn chat.
    Returns (text, error)."""
    fn = {'gemini': _call_gemini, 'openai': _call_openai}.get(get_provider(), _call_claude)
    delay = 1.0
    text, err = fn(prompt, system, max_tokens, history)
    for attempt in range(retries):
        if err is None or not _is_transient(err):
            break
        logger.warning('LLM transient overload (retry %d/%d): %s',
                       attempt + 1, retries, str(err)[:120])
        time.sleep(delay)
        delay = min(delay * 2, 8)
        text, err = fn(prompt, system, max_tokens, history)
    return text, err


def _history_messages(history):
    """Normalize a history list into clean [{'role','content'}] turns (user/
    assistant only, non-empty), shared by the Claude and OpenAI message builders."""
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


def can_use_tools() -> bool:
    """True when the agentic (tool-use) path is available — the provider is Claude
    or OpenAI and its SDK is importable. Tool use lets the assistant drill into any
    order / shift / date / cashier / product on demand (and compare arbitrary date
    ranges) instead of being limited to a fixed pre-computed snapshot."""
    provider = get_provider()
    if provider == 'openai':
        return openai is not None
    if provider == 'claude':
        return anthropic is not None
    return False


def call_ai_tools(prompt, system=None, tools=None, tool_executor=None,
                  max_tokens=4096, max_iterations=10, retries=2, history=None):
    """Run the model (Claude or OpenAI) in a tool-use loop so it can read the live
    database in full detail — drill into any order/shift/date/cashier/product and
    compare arbitrary date ranges. `tools` is a list of Anthropic-style tool schemas
    ({name, description, input_schema}); `tool_executor(name, input_dict)` executes
    one tool call and returns its result as a string (typically JSON). The loop
    feeds tool results back until the model answers with text or the iteration
    budget is spent.

    Claude and OpenAI run tools. If the provider is something else, the SDK is
    missing, or no tools/executor were supplied, this falls back to a single
    `call_ai()` so the caller never has to branch. Returns (text, error) with the
    same error codes as `call_ai` ('llm_key_missing' / 'llm_sdk_missing' / raw)."""
    if (not can_use_tools() or not tools or tool_executor is None):
        return call_ai(prompt, system=system, max_tokens=max_tokens,
                       retries=retries, history=history)

    if get_provider() == 'openai':
        return _openai_tool_loop(prompt, system, tools, tool_executor,
                                 max_tokens, max_iterations, retries, history)

    # ── Claude tool-use loop ──
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or ''
    if not api_key:
        return None, 'llm_key_missing'
    model = getattr(settings, 'ANTHROPIC_MODEL', '') or DEFAULT_CLAUDE_MODEL

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=_timeout_seconds())
    except Exception as e:  # noqa: BLE001
        logger.exception('claude client init failed')
        return None, str(e)

    messages = _history_messages(history) + [{'role': 'user', 'content': prompt}]

    def _create(include_tools):
        # One create() call, retrying transient provider overloads (529 / 'high
        # demand') with backoff — same policy as call_ai's single-shot path.
        kwargs = {'model': model, 'max_tokens': max_tokens, 'messages': messages}
        if system:
            kwargs['system'] = system
        if include_tools:
            kwargs['tools'] = tools
        delay = 1.0
        last_err = None
        for attempt in range(retries + 1):
            try:
                return client.messages.create(**kwargs), None
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                if attempt < retries and _is_transient(last_err):
                    logger.warning('claude transient overload (retry %d/%d): %s',
                                   attempt + 1, retries, last_err[:120])
                    time.sleep(delay)
                    delay = min(delay * 2, 8)
                    continue
                return None, last_err
        return None, last_err

    def _text(resp):
        return ''.join(
            b.text for b in resp.content if getattr(b, 'type', None) == 'text'
        )

    try:
        for _ in range(max_iterations):
            resp, err = _create(include_tools=True)
            if err:
                return None, err
            if getattr(resp, 'stop_reason', None) != 'tool_use':
                return _text(resp), None

            # Echo the assistant turn (incl. tool_use blocks), then run every
            # requested tool and return all results in one user turn.
            messages.append({'role': 'assistant', 'content': resp.content})
            results = []
            for block in resp.content:
                if getattr(block, 'type', None) != 'tool_use':
                    continue
                try:
                    out = tool_executor(block.name, dict(block.input or {}))
                    results.append({
                        'type': 'tool_result', 'tool_use_id': block.id,
                        'content': out if isinstance(out, str) else str(out),
                    })
                except Exception as e:  # noqa: BLE001
                    logger.exception('AI tool %s failed', getattr(block, 'name', '?'))
                    results.append({
                        'type': 'tool_result', 'tool_use_id': block.id,
                        'content': f'Tool error: {e}', 'is_error': True,
                    })
            messages.append({'role': 'user', 'content': results})

        # Iteration budget spent while still calling tools: ask once more with
        # tools withheld so the model must answer from what it has gathered.
        resp, err = _create(include_tools=False)
        if err:
            return None, err
        return _text(resp), None
    except Exception as e:  # noqa: BLE001
        logger.exception('claude tool loop failed')
        return None, str(e)


def _call_claude(prompt, system, max_tokens, history=None):
    if anthropic is None:
        return None, 'llm_sdk_missing'
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or ''
    if not api_key:
        return None, 'llm_key_missing'
    model = getattr(settings, 'ANTHROPIC_MODEL', '') or DEFAULT_CLAUDE_MODEL
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=_timeout_seconds())
        kwargs = {
            'model': model,
            'max_tokens': max_tokens,
            'messages': _history_messages(history) + [{'role': 'user', 'content': prompt}],
        }
        if system:
            kwargs['system'] = system
        resp = client.messages.create(**kwargs)
        # content is a list of blocks; concatenate the text blocks. No sampling
        # params are sent so this stays valid across the Opus 4.x line too.
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
    # Gemini has no separate system / role fields in this simple call — fold the
    # system prompt and any prior conversation turns into one text blob.
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
        cfg = types.GenerateContentConfig(max_output_tokens=max_tokens)
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
        client = openai.OpenAI(api_key=api_key, timeout=_timeout_seconds())
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.extend(_history_messages(history))
        messages.append({'role': 'user', 'content': prompt})
        # GPT-5-class models reject the legacy `max_tokens` and bill reasoning +
        # answer against `max_completion_tokens`; keep a generous floor so an
        # answer is never truncated (or eaten entirely by reasoning).
        ceiling = max(int(max_tokens or 2048), OPENAI_MIN_COMPLETION_TOKENS)
        resp = client.chat.completions.create(
            model=model, messages=messages, max_completion_tokens=ceiling,
        )
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
                      max_iterations, retries, history):
    """OpenAI function-calling loop — the OpenAI twin of the Claude tool loop. The
    model calls read-only data tools to answer in full detail (compare dates, drill
    into any order/shift/cashier/product). Returns (text, error)."""
    api_key = getattr(settings, 'OPENAI_API_KEY', '') or ''
    if not api_key:
        return None, 'llm_key_missing'
    model = getattr(settings, 'OPENAI_MODEL', '') or DEFAULT_OPENAI_MODEL
    try:
        client = openai.OpenAI(api_key=api_key, timeout=_timeout_seconds())
    except Exception as e:  # noqa: BLE001
        logger.exception('openai client init failed')
        return None, str(e)

    # Anthropic-style tool schema -> OpenAI function schema (input_schema is already
    # a JSON Schema, which is exactly what OpenAI's `parameters` expects).
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

    def _create(include_tools):
        kwargs = {'model': model, 'messages': messages, 'max_completion_tokens': ceiling}
        if include_tools:
            kwargs['tools'] = oai_tools
        delay, last_err = 1.0, None
        for attempt in range(retries + 1):
            try:
                return client.chat.completions.create(**kwargs), None
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                if attempt < retries and _is_transient(last_err):
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
                return _final_text(resp)
            # Echo the assistant tool-call turn, then run every requested tool and
            # append one tool-result message per call.
            messages.append({
                'role': 'assistant',
                'content': msg.content or '',
                'tool_calls': [{
                    'id': tc.id, 'type': 'function',
                    'function': {'name': tc.function.name,
                                 'arguments': tc.function.arguments or '{}'},
                } for tc in tool_calls],
            })
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or '{}')
                except (ValueError, TypeError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                try:
                    out = tool_executor(tc.function.name, args)
                except Exception as e:  # noqa: BLE001
                    logger.exception('AI tool %s failed', tc.function.name)
                    out = f'Tool error: {e}'
                messages.append({
                    'role': 'tool', 'tool_call_id': tc.id,
                    'content': out if isinstance(out, str) else str(out),
                })

        # Iteration budget spent: ask once more without tools so the model answers.
        resp, err = _create(include_tools=False)
        if err:
            return None, err
        return _final_text(resp)
    except Exception as e:  # noqa: BLE001
        logger.exception('openai tool loop failed')
        return None, str(e)
