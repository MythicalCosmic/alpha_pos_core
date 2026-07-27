"""Resolve the operational branch visible to an authenticated actor.

Cloud admin identities are global (their ``branch_id`` is commonly ``cloud``),
while transactional rows belong to a concrete restaurant branch.  Read and
write endpoints must therefore resolve one explicit operational branch instead
of accidentally querying every branch or creating cloud-owned business rows.
"""

from django.conf import settings


def resolve_actor_branch(actor=None):
    """Return one authorized operational branch id, or ``None``.

    A concrete branch carried by the actor wins.  Global cloud actors use the
    configured single-branch target.  Local installations use their bound
    ``BRANCH_ID``.  As a compatibility fallback, a cloud with exactly one live
    cash register may infer that register's branch; multiple branches fail
    closed.
    """

    actor_branch = str(getattr(actor, "branch_id", "") or "").strip()
    deployment_mode = str(
        getattr(settings, "DEPLOYMENT_MODE", "local") or "local"
    ).strip().lower()
    node_branch = str(getattr(settings, "BRANCH_ID", "") or "").strip()

    if deployment_mode != "cloud":
        return actor_branch or node_branch or None

    global_markers = {"", "cloud"}
    if node_branch:
        global_markers.add(node_branch)
    if actor_branch not in global_markers:
        return actor_branch

    configured = str(
        getattr(settings, "CLOUD_DEFAULT_TARGET_BRANCH_ID", "") or ""
    ).strip()
    if configured:
        return configured

    # Import lazily so this helper stays cheap for the common configured path
    # and does not create a model import cycle during Django app startup.
    from base.models import CashRegister

    candidates = list(
        CashRegister.objects.filter(is_deleted=False)
        .exclude(branch_id__in=global_markers)
        .order_by("branch_id")
        .values_list("branch_id", flat=True)
        .distinct()[:2]
    )
    if len(candidates) == 1:
        return str(candidates[0]).strip() or None
    return None

