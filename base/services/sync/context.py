from contextlib import contextmanager
from contextvars import ContextVar


_authoritative_cloud_pull = ContextVar(
    'authoritative_cloud_pull',
    default=False,
)


def is_authoritative_cloud_pull():
    """Whether the current record is being applied by a trusted cloud pull."""
    return bool(_authoritative_cloud_pull.get())


@contextmanager
def authoritative_cloud_pull(enabled=True):
    """Scope cloud authority to the authenticated pull call stack."""
    # Always set the requested value. A nested untrusted/direct apply must not
    # inherit authority merely because a trusted pull happens to be its caller.
    token = _authoritative_cloud_pull.set(bool(enabled))
    try:
        yield
    finally:
        _authoritative_cloud_pull.reset(token)
