"""Operational branch checks shared by stock document endpoints."""
from functools import wraps

from django.http import JsonResponse

from base.services.branch_scope import resolve_actor_branch


def stock_branch_required(model=None, id_argument=None):
    def decorate(view):
        @wraps(view)
        def scoped(request, *args, **kwargs):
            branch = str(resolve_actor_branch(request.user) or '').strip()
            if not branch:
                return JsonResponse({'success': False, 'code': 'BRANCH_SCOPE_REQUIRED',
                                     'message': 'An operational branch is required.'}, status=403)
            request.stock_branch_id = branch
            if model is not None:
                document_id = kwargs.get(id_argument, args[0] if args else None)
                row = model.objects.filter(pk=document_id, is_deleted=False,
                                           branch_id=branch).first()
                if row is None:
                    return JsonResponse({'success': False, 'code': 'STOCK_SCOPE_FORBIDDEN',
                                         'message': 'Stock document is outside the authorized branch.'}, status=403)
                request.stock_document = row
            return view(request, *args, **kwargs)
        return scoped
    return decorate
