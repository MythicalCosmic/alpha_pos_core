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
    OPENAI_MODEL       — defaults to gpt-5.6-luna (cost-optimized GPT-5.6).
    OPENAI_REASONING_EFFORT — defaults to low for GPT-5.6 models.

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
DEFAULT_OPENAI_MODEL = 'gpt-5.6-luna'
# GPT-5-class models bill reasoning + answer against one ceiling and use
# `max_completion_tokens` (not the legacy `max_tokens`); keep a generous floor so
# a long answer (or any internal reasoning) is never truncated mid-sentence. The
# GPT-5 reasoning models need real headroom; a truncated reasoning pass returns
# empty content -> 'openai_empty_response'.
OPENAI_MIN_COMPLETION_TOKENS = 8192

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
    'timeout', 'timed out', 'connection error', 'connection reset',
    'connection aborted', 'server disconnected', 'temporarily unavailable',
)

# Errors that *look* transient (often carry a 429) but won't recover by retrying:
# an account with no credits/billing, or a bad key. Fail fast instead of burning
# the backoff budget — the operator must fix billing / the key.
_HARD_MARKERS = (
    'insufficient_quota', 'exceeded your current quota', 'billing',
    'credit balance', 'purchase credits',
    'invalid api key', 'incorrect api key', 'invalid_api_key',
)

# Provider errors that should be explained to the operator specifically as a
# provider-side capacity/rate-limit problem. Keep this narrower than
# ``_TRANSIENT_MARKERS``: a plain network timeout or generic 503 is retryable but
# is not necessarily a rate limit. SDKs spell the same condition in several
# ways (OpenAI/Anthropic use 429/529; Gemini commonly uses RESOURCE_EXHAUSTED or
# "high demand").
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

    # ── Claude tool-use loop ──
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
        # One create() call, retrying transient provider overloads (529 / 'high
        # demand') with backoff — same policy as call_ai's single-shot path.
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

            # Echo the assistant turn (incl. tool_use blocks), then run every
            # requested tool and return all results in one user turn.
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


# ── AI determinism + prompt caching helpers ─────────────────────────────────
# Learned once per process: whether the OpenAI endpoint/model accepts our
# determinism/cache kwargs. None = not yet probed; False = rejected once, so we
# stop sending them (avoids a failed+retry round-trip on every request).
_OPENAI_EXTRAS = ('seed', 'prompt_cache_key', 'temperature')
_openai_extras_ok = None


def _openai_sampling_kwargs(model):
    """Determinism + cache-routing kwargs for the OpenAI chat API. `seed` gives
    best-effort reproducibility (same question -> same answer); `prompt_cache_key`
    improves prefix-cache hit routing (caching itself is automatic once the static
    system prefix is large, which it is). Reasoning models (gpt-5 / o-series) reject
    a non-default temperature, so temperature is only sent for classic models.
    Anything the SDK/model rejects is stripped by _openai_create."""
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
    """client.chat.completions.create(**kwargs), tolerant of an SDK/model that
    rejects the determinism/cache kwargs: an old SDK raises TypeError, a stricter
    model returns 400 'unsupported_parameter/value'. On that specific failure we
    strip the extras, remember it for the rest of the process, and retry once so
    the call still succeeds. Every other error propagates unchanged."""
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
    """Wrap the static system prompt in an Anthropic cache_control block so the
    large prefix is cached (ephemeral). Plain string in -> list-of-one-block out;
    falsy stays falsy."""
    if not system:
        return system
    return [{'type': 'text', 'text': system,
             'cache_control': {'type': 'ephemeral'}}]


def _cache_tools(tools):
    """Mark the LAST tool with cache_control so the whole tools array caches with
    the system prefix. Returns a shallow copy (never mutates the caller's list)."""
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
    """OpenAI function-calling loop — the OpenAI twin of the Claude tool loop. The
    model calls read-only data tools to answer in full detail (compare dates, drill
    into any order/shift/cashier/product). Returns (text, error)."""
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
