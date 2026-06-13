"""Sandboxed str.format for admin-editable notification templates.

Python's stock `str.format` is famously unsafe with attacker-controlled
template strings: `{x.__class__.__init__.__globals__[os].environ}` reads
process env, and `{x.__init__.__class__.__bases__[0]}` walks the type
hierarchy. Our notification templates are admin-editable through the API,
so we cannot pass them through `str.format(**ctx)` directly even though
the editor is gated behind @admin_required — admin-credential compromise
should not become env-variable exfiltration.

`SafeFormatter` accepts the same `{name}` / `{name:fmt}` / `{name!s}`
shapes as the standard formatter but refuses any field name containing
`.` (attribute access) or `[` (index/key access). That keeps the existing
template syntax — no migration of stored rows — while closing the escape.
"""
import string


class _UnsafePlaceholder(ValueError):
    """Raised when a template's placeholder reaches outside the context dict
    via attribute or index access. Caller decides whether to surface the
    failure (template editor) or swallow it (send pipeline)."""


class SafeFormatter(string.Formatter):
    """Drop-in replacement for `str.format` that whitelists field names.

    Stock Formatter resolves `{a.b}` via `getattr(ctx['a'], 'b')` and
    `{a[k]}` via `ctx['a'][k]`. Both are attacker-reachable on any
    object the template references — including the brand string we
    inject ourselves. Refuse them entirely.
    """

    def get_field(self, field_name, args, kwargs):
        if '.' in field_name or '[' in field_name:
            raise _UnsafePlaceholder(
                f'placeholder {{{field_name}}} uses attribute or index access'
            )
        return super().get_field(field_name, args, kwargs)


def safe_format(template, /, **context):
    """Render `template` with `context`, raising on attribute/index access.

    Use this anywhere a stored template string is rendered against a runtime
    context dict. For preview/validation paths, catch _UnsafePlaceholder and
    return a user-facing error; for the send pipeline, the caller should
    log+drop the notification rather than risk a partial render.
    """
    return SafeFormatter().vformat(template, (), context)


def validate_template_text(template_text):
    """Return None if the template is safe to store, else an error string.

    Catches the same unsafe placeholders `safe_format` would refuse at
    render time, so admins see the rejection at write time instead of every
    event of that notification_type silently dropping.
    """
    if not isinstance(template_text, str):
        return 'template_text must be a string'
    try:
        parsed = list(string.Formatter().parse(template_text))
    except (ValueError, IndexError) as exc:
        return f'invalid template syntax: {exc}'
    for _literal, field_name, _spec, _conv in parsed:
        if field_name is None:
            continue
        if field_name == '':
            return 'template uses positional {} placeholder; use named {field}'
        if field_name.isdigit():
            return 'template uses indexed {0} placeholder; use named {field}'
        if '.' in field_name or '[' in field_name:
            return (
                f'placeholder {{{field_name}}} uses attribute or index access; '
                'only plain {name} substitutions are allowed'
            )
    return None
