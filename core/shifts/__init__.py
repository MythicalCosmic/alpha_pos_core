"""ShiftService, relocated here from ``admins.services.shift_service`` so the local
till's shift open/close (``customers/views/shift_views.py``) no longer imports a
server-side app. Operates on shared-core models only (base.Shift / ShiftTemplate,
cashbox.ShiftPaymentTotal). ``admins`` keeps only the analytics layer.

TODO(Phase 1): move the implementation here and repoint importers.
"""
