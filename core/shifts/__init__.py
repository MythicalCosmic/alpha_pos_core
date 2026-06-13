"""ShiftService relocated here from admins.services.shift_service so the local till
(customers/views/shift_views.py) doesn't import a server-only app. Operates on
shared-core models only (base.Shift/ShiftTemplate, cashbox.ShiftPaymentTotal).
"""
from core.shifts.service import ShiftService, ShiftTemplateService  # noqa: F401
