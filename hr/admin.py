from django.contrib import admin
from .models import (
    Department, Employee, ExpenseCategory, Expense, SalaryPayment,
    CashTransaction, EmployeeContract, ContractDocument, LeaveType,
    LeaveRequest, LeaveBalance, Attendance, EmployeeDocument,
    PerformanceReview, PerformanceGoal, EmploymentEvent,
)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'manager', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'department', 'position', 'contract_type', 'is_active')
    list_filter = ('contract_type', 'is_active', 'department')
    search_fields = ('user__first_name', 'user__last_name', 'position')
    autocomplete_fields = ('user', 'department')


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'budget_limit', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ('id', 'category', 'amount', 'status', 'payment_method', 'expense_date')
    list_filter = ('status', 'payment_method', 'category')
    date_hierarchy = 'expense_date'


@admin.register(SalaryPayment)
class SalaryPaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'period_year', 'period_month', 'net_amount', 'status')
    list_filter = ('status', 'period_year', 'period_month')


@admin.register(CashTransaction)
class CashTransactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'type', 'amount', 'payment_method', 'balance_before', 'balance_after', 'created_at')
    list_filter = ('type', 'payment_method')
    date_hierarchy = 'created_at'


@admin.register(EmployeeContract)
class EmployeeContractAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'contract_number', 'contract_type', 'status', 'start_date', 'end_date')
    list_filter = ('status', 'contract_type')
    search_fields = ('contract_number',)


@admin.register(ContractDocument)
class ContractDocumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'contract', 'title', 'document_type', 'uploaded_at')
    list_filter = ('document_type',)


@admin.register(LeaveType)
class LeaveTypeAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'is_paid', 'annual_quota', 'requires_approval', 'is_active')
    list_filter = ('is_paid', 'is_active')


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'leave_type', 'start_date', 'end_date', 'days_count', 'status')
    list_filter = ('status', 'leave_type')
    date_hierarchy = 'start_date'


@admin.register(LeaveBalance)
class LeaveBalanceAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'leave_type', 'year', 'allocated_days', 'used_days')
    list_filter = ('year', 'leave_type')


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'date', 'status', 'source', 'work_hours')
    list_filter = ('status', 'source')
    date_hierarchy = 'date'


@admin.register(EmployeeDocument)
class EmployeeDocumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'title', 'document_type', 'is_verified', 'uploaded_at')
    list_filter = ('document_type', 'is_verified')


@admin.register(PerformanceReview)
class PerformanceReviewAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'reviewer', 'rating', 'status', 'review_period_start', 'review_period_end')
    list_filter = ('status', 'rating')


@admin.register(PerformanceGoal)
class PerformanceGoalAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'title', 'status', 'progress_percent', 'target_date')
    list_filter = ('status',)


@admin.register(EmploymentEvent)
class EmploymentEventAdmin(admin.ModelAdmin):
    list_display = ('id', 'employee', 'event_type', 'event_date')
    list_filter = ('event_type',)
    date_hierarchy = 'event_date'
