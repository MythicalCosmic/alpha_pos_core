"""Shared accounting classification used by POS, HR, and cloud reporting."""

from django.db import models


class FinancialReportingGroup(models.TextChoices):
    INVENTORY_PURCHASE = "INVENTORY_PURCHASE", "Inventory purchase"
    PAYROLL = "PAYROLL", "Payroll"
    RENT = "RENT", "Rent"
    UTILITIES = "UTILITIES", "Utilities"
    OPERATING = "OPERATING", "Operating expense"
    WASTE_SPOILAGE = "WASTE_SPOILAGE", "Waste and spoilage"
    FINANCE_FEES = "FINANCE_FEES", "Finance fees"
    DEPRECIATION = "DEPRECIATION", "Depreciation"
    TAXES = "TAXES", "Taxes"
    CAPITAL_EXPENDITURE = "CAPITAL_EXPENDITURE", "Capital expenditure"
    OWNER_DRAW = "OWNER_DRAW", "Owner withdrawal"
    NON_BUSINESS = "NON_BUSINESS", "Non-business movement"
    OTHER_INCOME = "OTHER_INCOME", "Other income"
    REVIEW = "REVIEW", "Needs review"


PROFIT_EXPENSE_GROUPS = frozenset({
    FinancialReportingGroup.PAYROLL,
    FinancialReportingGroup.RENT,
    FinancialReportingGroup.UTILITIES,
    FinancialReportingGroup.OPERATING,
    FinancialReportingGroup.WASTE_SPOILAGE,
    FinancialReportingGroup.FINANCE_FEES,
    FinancialReportingGroup.DEPRECIATION,
    FinancialReportingGroup.TAXES,
})

CASH_ONLY_GROUPS = frozenset({
    FinancialReportingGroup.INVENTORY_PURCHASE,
    FinancialReportingGroup.CAPITAL_EXPENDITURE,
    FinancialReportingGroup.OWNER_DRAW,
    FinancialReportingGroup.NON_BUSINESS,
})

EXPENSE_REPORTING_GROUPS = frozenset(
    PROFIT_EXPENSE_GROUPS
    | CASH_ONLY_GROUPS
    | {FinancialReportingGroup.REVIEW}
)
