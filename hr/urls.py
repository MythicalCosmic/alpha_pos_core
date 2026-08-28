from django.urls import path
from hr.views import department_views, employee_views, expense_views, salary_views, cash_views
from hr.views import contract_views, leave_views, attendance_views, document_views, review_views, event_views
from hr.views import operational_audit_views

app_name = 'hr'

urlpatterns = [
    # Departments
    path('departments/', department_views.departments, name='department-list'),
    path('departments/<int:department_id>/', department_views.department_detail, name='department-detail'),

    # Employees
    path('employees/', employee_views.employees, name='employee-list'),
    path('employees/stats/', employee_views.employee_stats, name='employee-stats'),
    path('employees/<int:employee_id>/', employee_views.employee_detail, name='employee-detail'),

    # Expense Categories
    path('expense-categories/', expense_views.expense_categories, name='expense-category-list'),
    path('expense-categories/<int:category_id>/', expense_views.expense_category_detail, name='expense-category-detail'),

    # Expenses
    path('expenses/', expense_views.expenses, name='expense-list'),
    path('expenses/stats/', expense_views.expense_stats, name='expense-stats'),
    path('expenses/<int:expense_id>/', expense_views.expense_detail, name='expense-detail'),
    path('expenses/<int:expense_id>/approve/', expense_views.expense_approve, name='expense-approve'),
    path('expenses/<int:expense_id>/reject/', expense_views.expense_reject, name='expense-reject'),
    path('expenses/<int:expense_id>/pay/', expense_views.expense_pay, name='expense-pay'),

    # Salary
    path('salaries/', salary_views.salaries, name='salary-list'),
    path('salaries/generate/', salary_views.salary_generate, name='salary-generate'),
    path('salaries/approve-all/', salary_views.salary_approve_all, name='salary-approve-all'),
    path('salaries/summary/', salary_views.salary_summary, name='salary-summary'),
    path('salaries/<int:salary_id>/', salary_views.salary_detail, name='salary-detail'),
    path('salaries/<int:salary_id>/approve/', salary_views.salary_approve, name='salary-approve'),
    path('salaries/<int:salary_id>/pay/', salary_views.salary_pay, name='salary-pay'),
    path('salaries/<int:salary_id>/base/', salary_views.salary_set_base, name='salary-set-base'),
    path('salaries/<int:salary_id>/bonuses/', salary_views.salary_bonuses, name='salary-bonuses'),
    path('salaries/<int:salary_id>/deductions/', salary_views.salary_deductions, name='salary-deductions'),
    path('salaries/<int:salary_id>/bonuses/<int:bonus_id>/', salary_views.salary_bonus_delete, name='salary-bonus-delete'),
    path('salaries/<int:salary_id>/deductions/<int:deduction_id>/', salary_views.salary_deduction_delete, name='salary-deduction-delete'),

    # Cash
    path('cash/', cash_views.cash_transactions, name='cash-list'),
    path('cash/deposit/', cash_views.cash_deposit, name='cash-deposit'),
    path('cash/withdraw/', cash_views.cash_withdraw, name='cash-withdraw'),
    path('cash/balance/', cash_views.cash_balance, name='cash-balance'),
    path('cash/<int:transaction_id>/', cash_views.cash_transaction_detail, name='cash-detail'),

    # Contracts
    path('contracts/', contract_views.contracts, name='contract-list'),
    path('contracts/expiring/', contract_views.contracts_expiring, name='contract-expiring'),
    path('contracts/<int:contract_id>/', contract_views.contract_detail, name='contract-detail'),
    path('contracts/<int:contract_id>/activate/', contract_views.contract_activate, name='contract-activate'),
    path('contracts/<int:contract_id>/terminate/', contract_views.contract_terminate, name='contract-terminate'),
    path('contracts/<int:contract_id>/renew/', contract_views.contract_renew, name='contract-renew'),
    path('contracts/<int:contract_id>/documents/', contract_views.contract_documents, name='contract-documents'),
    path('contracts/<int:contract_id>/documents/<int:doc_id>/', contract_views.contract_document_detail, name='contract-document-detail'),

    # Leave
    path('leave-types/', leave_views.leave_types, name='leave-type-list'),
    path('leave-types/<int:type_id>/', leave_views.leave_type_detail, name='leave-type-detail'),
    path('leaves/', leave_views.leave_requests, name='leave-list'),
    path('leaves/calendar/', leave_views.leave_calendar, name='leave-calendar'),
    path('leaves/<int:leave_id>/', leave_views.leave_detail, name='leave-detail'),
    path('leaves/<int:leave_id>/approve/', leave_views.leave_approve, name='leave-approve'),
    path('leaves/<int:leave_id>/reject/', leave_views.leave_reject, name='leave-reject'),
    path('leaves/<int:leave_id>/cancel/', leave_views.leave_cancel, name='leave-cancel'),
    path('leave-balances/', leave_views.leave_balances, name='leave-balance-list'),
    path('leave-balances/initialize/', leave_views.leave_balance_initialize, name='leave-balance-init'),
    path('leave-balances/employee/<int:employee_id>/', leave_views.leave_balance_by_employee, name='leave-balance-employee'),

    # Attendance
    path('attendance/', attendance_views.attendance_list, name='attendance-list'),
    path('attendance/check-in/', attendance_views.attendance_check_in, name='attendance-check-in'),
    path('attendance/check-out/', attendance_views.attendance_check_out, name='attendance-check-out'),
    path('attendance/daily-report/', attendance_views.attendance_daily_report, name='attendance-daily-report'),
    path('attendance/monthly-report/', attendance_views.attendance_monthly_report, name='attendance-monthly-report'),
    path('attendance/<int:attendance_id>/', attendance_views.attendance_detail, name='attendance-detail'),
    path('attendance/manual-entry/', operational_audit_views.attendance_manual_entry, name='attendance-manual-entry'),
    path('attendance/summary/', operational_audit_views.attendance_summary, name='attendance-summary'),
    path('attendance/<int:attendance_id>/adjustment-requests/', operational_audit_views.attendance_adjustment_request, name='attendance-adjustment-request'),
    path('attendance-adjustments/<int:request_id>/<str:action>/', operational_audit_views.attendance_adjustment_review, name='attendance-adjustment-review'),
    path('attendance/<int:attendance_id>/excuses/', operational_audit_views.attendance_excuse_create, name='attendance-excuse-create'),
    path('attendance-excuses/<int:excuse_id>/<str:action>/', operational_audit_views.attendance_excuse_review, name='attendance-excuse-review'),

    # Work schedules and operational discipline
    path('work-schedules/', operational_audit_views.work_schedules, name='work-schedule-list'),
    path('work-schedules/<int:schedule_id>/', operational_audit_views.work_schedule_detail, name='work-schedule-detail'),
    path('discipline-rules/', operational_audit_views.discipline_rules, name='discipline-rule-list'),
    path('discipline-rules/<int:rule_id>/', operational_audit_views.discipline_rule_detail, name='discipline-rule-detail'),
    path('discipline-cases/', operational_audit_views.discipline_cases, name='discipline-case-list'),
    path('discipline-cases/<int:case_id>/', operational_audit_views.discipline_case_detail, name='discipline-case-detail'),
    path('discipline-cases/<int:case_id>/<str:action>/', operational_audit_views.discipline_case_review, name='discipline-case-review'),

    # Central READY-event preparation audits
    path('preparation-audits/', operational_audit_views.preparation_audits, name='preparation-audit-list'),
    path('preparation-audits/<int:audit_id>/', operational_audit_views.preparation_audit_detail, name='preparation-audit-detail'),
    path('preparation-audit-categories/', operational_audit_views.preparation_audit_categories, name='preparation-audit-category-list'),
    path('preparation-audits/<int:audit_id>/review/', operational_audit_views.preparation_audit_review, name='preparation-audit-review'),
    path('preparation-audits/<int:audit_id>/reopen/', operational_audit_views.preparation_audit_reopen, name='preparation-audit-reopen'),
    path('audit-dashboard/', operational_audit_views.audit_dashboard, name='operational-audit-dashboard'),
    path('audit-periods/close/', operational_audit_views.audit_period_close, name='operational-audit-period-close'),

    # Documents
    path('documents/', document_views.documents, name='document-list'),
    path('documents/expiring/', document_views.documents_expiring, name='document-expiring'),
    path('documents/employee/<int:employee_id>/', document_views.documents_by_employee, name='document-by-employee'),
    path('documents/<int:doc_id>/', document_views.document_detail, name='document-detail'),
    path('documents/<int:doc_id>/verify/', document_views.document_verify, name='document-verify'),
    # Auth-gated file download. <kind> maps via _DOWNLOADABLE_FILES to a
    # specific (model, file_field) pair so the URL cannot be used to read
    # arbitrary files.
    path('documents/file/<str:kind>/<int:obj_id>/', document_views.secure_download, name='document-download'),

    # Reviews
    path('reviews/', review_views.reviews, name='review-list'),
    path('reviews/<int:review_id>/', review_views.review_detail, name='review-detail'),
    path('reviews/<int:review_id>/submit/', review_views.review_submit, name='review-submit'),
    path('reviews/<int:review_id>/acknowledge/', review_views.review_acknowledge, name='review-acknowledge'),

    # Goals
    path('goals/', review_views.goals, name='goal-list'),
    path('goals/<int:goal_id>/', review_views.goal_detail, name='goal-detail'),
    path('goals/<int:goal_id>/progress/', review_views.goal_progress, name='goal-progress'),

    # Events
    path('events/', event_views.events, name='event-list'),
    path('events/employee/<int:employee_id>/', event_views.employee_timeline, name='event-timeline'),
    path('events/<int:event_id>/', event_views.event_detail, name='event-detail'),
]
