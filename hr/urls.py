from django.urls import path
from hr.views import department_views, employee_views, expense_views, salary_views, cash_views
from hr.views import contract_views, leave_views, attendance_views, document_views, review_views, event_views

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
