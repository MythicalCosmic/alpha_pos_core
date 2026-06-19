"""Single entry point for LLM calls — Claude (Anthropic) or Gemini (Google).

Both the stock AI assistant and the demand forecaster call `call_ai()`, which
dispatches to whichever provider the operator selected. Everything is
operator-configured (desktop panel / env):

    AI_PROVIDER        — 'claude' (default) or 'gemini'.
    ANTHROPIC_API_KEY  — required when AI_PROVIDER=claude.
    ANTHROPIC_MODEL    — defaults to claude-sonnet-4-6 (also: claude-sonnet-4-5,
                         claude-opus-4-8).
    GEMINI_API_KEY     — required when AI_PROVIDER=gemini.
    GEMINI_MODEL       — defaults to gemini-2.5-flash.

Both backends return (text, error) where error is None on success, or one of
'llm_sdk_missing' / 'llm_key_missing' / a raw error string. The callers handle
those codes identically regardless of provider, so switching is a config change.
"""
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:
    anthropic = None

# Current Sonnet — same price as 4.5, 1M context. Override via ANTHROPIC_MODEL.
DEFAULT_CLAUDE_MODEL = 'claude-sonnet-4-6'
DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'

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


def _is_transient(err) -> bool:
    e = (err or '').lower()
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
    if get_provider() == 'gemini':
        return not (getattr(settings, 'GEMINI_API_KEY', '') or '')
    return not (getattr(settings, 'ANTHROPIC_API_KEY', '') or '')


def call_ai(prompt, system=None, max_tokens=2048, retries=2):
    """Dispatch to the configured provider, retrying transient provider overloads
    (Gemini flash 503 'high demand' / 429) with backoff. A 503 returns fast, so the
    retries cost little and turn a transient spike into a successful answer instead
    of 'AI assistant temporarily unavailable'. Returns (text, error)."""
    fn = _call_gemini if get_provider() == 'gemini' else _call_claude
    delay = 1.0
    text, err = fn(prompt, system, max_tokens)
    for attempt in range(retries):
        if err is None or not _is_transient(err):
            break
        logger.warning('LLM transient overload (retry %d/%d): %s',
                       attempt + 1, retries, str(err)[:120])
        time.sleep(delay)
        delay = min(delay * 2, 8)
        text, err = fn(prompt, system, max_tokens)
    return text, err


def can_use_tools() -> bool:
    """True when the agentic (tool-use) path is available: the Claude provider is
    selected and the anthropic SDK is importable. Tool use lets the assistant
    drill into any order/shift/date/cashier/product on demand instead of being
    limited to a fixed pre-computed snapshot — only Claude implements it here."""
    return get_provider() == 'claude' and anthropic is not None


def call_ai_tools(prompt, system=None, tools=None, tool_executor=None,
                  max_tokens=4096, max_iterations=10, retries=2):
    """Run Claude in a tool-use loop so it can read the live database in full
    detail. `tools` is a list of Anthropic tool schemas; `tool_executor(name,
    input_dict)` executes one tool call and returns its result as a string
    (typically JSON). The loop feeds tool results back until the model answers
    with text or the iteration budget is spent.

    Only the Claude provider runs tools. If the provider is not Claude, the SDK
    is missing, or no tools/executor were supplied, this falls back to a single
    `call_ai()` so the caller never has to branch. Returns (text, error) with the
    same error codes as `call_ai` ('llm_key_missing' / 'llm_sdk_missing' / raw)."""
    if (not can_use_tools() or not tools or tool_executor is None):
        return call_ai(prompt, system=system, max_tokens=max_tokens, retries=retries)

    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or ''
    if not api_key:
        return None, 'llm_key_missing'
    model = getattr(settings, 'ANTHROPIC_MODEL', '') or DEFAULT_CLAUDE_MODEL

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=_timeout_seconds())
    except Exception as e:  # noqa: BLE001
        logger.exception('claude client init failed')
        return None, str(e)

    messages = [{'role': 'user', 'content': prompt}]

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


def _call_claude(prompt, system, max_tokens):
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
            'messages': [{'role': 'user', 'content': prompt}],
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


def _call_gemini(prompt, system, max_tokens):
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, 'llm_sdk_missing'
    api_key = getattr(settings, 'GEMINI_API_KEY', '') or ''
    if not api_key:
        return None, 'llm_key_missing'
    model = getattr(settings, 'GEMINI_MODEL', '') or DEFAULT_GEMINI_MODEL
    # Gemini has no separate system field — prepend it to the prompt.
    contents = (system + '\n\n' + prompt) if system else prompt
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
