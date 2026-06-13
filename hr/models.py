from django.db import models
from base.models import SyncMixin, SyncManager


class Department(SyncMixin, models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    manager = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_departments',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['name']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['manager_uuid'] = str(self.manager.uuid) if self.manager else None
        return data

    def __str__(self):
        return self.name


class Employee(SyncMixin, models.Model):
    class ContractType(models.TextChoices):
        FULL_TIME = 'FULL_TIME', 'Full Time'
        PART_TIME = 'PART_TIME', 'Part Time'
        CONTRACT = 'CONTRACT', 'Contract'

    class PaymentFrequency(models.TextChoices):
        MONTHLY = 'MONTHLY', 'Monthly'
        WEEKLY = 'WEEKLY', 'Weekly'
        BI_WEEKLY = 'BI_WEEKLY', 'Bi-Weekly'

    user = models.OneToOneField(
        'base.User',
        on_delete=models.CASCADE,
        related_name='employee_profile',
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employees',
    )
    position = models.CharField(max_length=100)
    hire_date = models.DateField()
    contract_type = models.CharField(
        max_length=15,
        choices=ContractType.choices,
        default=ContractType.FULL_TIME,
    )
    base_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_frequency = models.CharField(
        max_length=10,
        choices=PaymentFrequency.choices,
        default=PaymentFrequency.MONTHLY,
    )
    phone = models.CharField(max_length=20, blank=True, default='')
    address = models.TextField(blank=True, default='')
    emergency_contact_name = models.CharField(max_length=100, blank=True, default='')
    emergency_contact_phone = models.CharField(max_length=20, blank=True, default='')
    bank_account = models.CharField(max_length=50, blank=True, default='')
    bank_name = models.CharField(max_length=100, blank=True, default='')
    status_tags = models.JSONField(
        default=list, blank=True,
        help_text="Tags: BLACKLIST, POSITIVE, NEGATIVE, WARNING, VIP",
    )
    medical_book_number = models.CharField(max_length=50, blank=True, default='')
    medical_book_expiry = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['user__first_name', 'user__last_name']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['user_uuid'] = str(self.user.uuid) if self.user else None
        data['department_uuid'] = str(self.department.uuid) if self.department else None
        return data

    def __str__(self):
        return f"{self.user.first_name} {self.user.last_name} - {self.position}"


class ExpenseCategory(SyncMixin, models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    budget_limit = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        verbose_name_plural = 'expense categories'
        ordering = ['name']

    def __str__(self):
        return self.name


class Expense(SyncMixin, models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = 'CASH', 'Cash'
        UZCARD = 'UZCARD', 'Uzcard'
        HUMO = 'HUMO', 'Humo'
        PAYME = 'PAYME', 'Payme'
        BANK_TRANSFER = 'BANK_TRANSFER', 'Bank Transfer'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        PAID = 'PAID', 'Paid'

    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expenses',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.TextField(blank=True, default='')
    expense_date = models.DateField()
    payment_method = models.CharField(
        max_length=15,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    receipt_number = models.CharField(max_length=100, blank=True, default='')
    receipt_image_url = models.URLField(
        max_length=500, blank=True, default='',
        help_text='DEPRECATED: legacy URL. New uploads should use receipt_file.',
    )
    receipt_file = models.FileField(
        upload_to='hr/expenses/%Y/%m/', blank=True, null=True,
        help_text='Private receipt file. Served via auth-gated download endpoint only.',
    )
    created_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_expenses',
    )
    approved_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_expenses',
    )
    paid_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='paid_expenses',
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-expense_date', '-created_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['expense_category_uuid'] = str(self.category.uuid) if self.category else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        data['approved_by_uuid'] = str(self.approved_by.uuid) if self.approved_by else None
        data['paid_by_uuid'] = str(self.paid_by.uuid) if self.paid_by else None
        return data

    def __str__(self):
        return f"Expense #{self.id} - {self.amount} ({self.status})"


class SalaryPayment(SyncMixin, models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        PAID = 'PAID', 'Paid'

    class PaymentMethod(models.TextChoices):
        CASH = 'CASH', 'Cash'
        UZCARD = 'UZCARD', 'Uzcard'
        HUMO = 'HUMO', 'Humo'
        PAYME = 'PAYME', 'Payme'
        BANK_TRANSFER = 'BANK_TRANSFER', 'Bank Transfer'

    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='salary_payments',
    )
    period_year = models.PositiveIntegerField()
    period_month = models.PositiveSmallIntegerField()
    base_amount = models.DecimalField(max_digits=12, decimal_places=2)
    bonus = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    deduction = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    payment_method = models.CharField(
        max_length=15,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_salaries',
    )
    created_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_salaries',
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        unique_together = ['employee', 'period_year', 'period_month']
        ordering = ['-period_year', '-period_month']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        data['approved_by_uuid'] = str(self.approved_by.uuid) if self.approved_by else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        return data

    def __str__(self):
        return f"Salary: {self.employee} - {self.period_year}/{self.period_month}"


class SalaryBonus(SyncMixin, models.Model):
    """One itemized bonus line on a month's salary (amount + reason). The scalar
    SalaryPayment.bonus is kept in sync (= Σ of these) for back-compat."""
    salary = models.ForeignKey(
        SalaryPayment, on_delete=models.CASCADE, related_name='bonuses')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    SYNC_WRITE_DENYLIST = frozenset({'amount'})
    objects = SyncManager()

    class Meta:
        ordering = ['created_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['salary_uuid'] = str(self.salary.uuid) if self.salary else None
        return data

    def __str__(self):
        return f"Bonus {self.amount} ({self.reason})"


class SalaryDeduction(SyncMixin, models.Model):
    """One itemized penalty line on a month's salary (amount + reason)."""
    salary = models.ForeignKey(
        SalaryPayment, on_delete=models.CASCADE, related_name='deductions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    SYNC_WRITE_DENYLIST = frozenset({'amount'})
    objects = SyncManager()

    class Meta:
        ordering = ['created_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['salary_uuid'] = str(self.salary.uuid) if self.salary else None
        return data

    def __str__(self):
        return f"Penalty {self.amount} ({self.reason})"


class CashTransaction(SyncMixin, models.Model):
    class TransactionType(models.TextChoices):
        DEPOSIT = 'DEPOSIT', 'Deposit'
        WITHDRAWAL = 'WITHDRAWAL', 'Withdrawal'
        EXPENSE_PAYMENT = 'EXPENSE_PAYMENT', 'Expense Payment'
        SALARY_PAYMENT = 'SALARY_PAYMENT', 'Salary Payment'

    class PaymentMethod(models.TextChoices):
        CASH = 'CASH', 'Cash'
        UZCARD = 'UZCARD', 'Uzcard'
        HUMO = 'HUMO', 'Humo'
        PAYME = 'PAYME', 'Payme'
        BANK_TRANSFER = 'BANK_TRANSFER', 'Bank Transfer'

    type = models.CharField(max_length=20, choices=TransactionType.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.TextField(blank=True, default='')
    payment_method = models.CharField(
        max_length=15,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
    )
    reference_type = models.CharField(max_length=50, blank=True, default='')
    reference_id = models.PositiveIntegerField(null=True, blank=True)
    balance_before = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    performed_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='cash_transactions',
    )
    approved_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_cash_transactions',
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-created_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['performed_by_uuid'] = str(self.performed_by.uuid) if self.performed_by else None
        data['approved_by_uuid'] = str(self.approved_by.uuid) if self.approved_by else None
        return data

    def __str__(self):
        return f"{self.type} - {self.amount} ({self.created_at})"


class EmployeeContract(SyncMixin, models.Model):
    class ContractType(models.TextChoices):
        INITIAL = 'INITIAL', 'Initial'
        RENEWAL = 'RENEWAL', 'Renewal'
        AMENDMENT = 'AMENDMENT', 'Amendment'

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        ACTIVE = 'ACTIVE', 'Active'
        EXPIRED = 'EXPIRED', 'Expired'
        TERMINATED = 'TERMINATED', 'Terminated'
        RENEWED = 'RENEWED', 'Renewed'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='contracts')
    contract_number = models.CharField(max_length=50, unique=True)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    probation_end_date = models.DateField(null=True, blank=True)
    contract_type = models.CharField(max_length=10, choices=ContractType.choices, default=ContractType.INITIAL)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    salary_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    position_title = models.CharField(max_length=100, blank=True, default='')
    terms = models.TextField(blank=True, default='')
    termination_date = models.DateField(null=True, blank=True)
    termination_reason = models.TextField(blank=True, default='')
    renewed_from = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='renewals',
    )
    created_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='created_contracts',
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-start_date']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        data['renewed_from_uuid'] = str(self.renewed_from.uuid) if self.renewed_from else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        return data

    def __str__(self):
        return f"Contract {self.contract_number} - {self.employee}"


class ContractDocument(SyncMixin, models.Model):
    class DocumentType(models.TextChoices):
        CONTRACT = 'CONTRACT', 'Contract'
        AMENDMENT = 'AMENDMENT', 'Amendment'
        TERMINATION = 'TERMINATION', 'Termination'
        OTHER = 'OTHER', 'Other'

    contract = models.ForeignKey(EmployeeContract, on_delete=models.CASCADE, related_name='documents')
    title = models.CharField(max_length=200)
    document_type = models.CharField(max_length=12, choices=DocumentType.choices, default=DocumentType.CONTRACT)
    file_url = models.URLField(
        max_length=500, blank=True, default='',
        help_text='DEPRECATED: legacy URL. New uploads should use file.',
    )
    file = models.FileField(
        upload_to='hr/contracts/%Y/%m/', blank=True, null=True,
        help_text='Private contract document. Served via auth-gated download endpoint only.',
    )
    uploaded_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='uploaded_contract_docs',
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-uploaded_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['contract_uuid'] = str(self.contract.uuid) if self.contract else None
        data['uploaded_by_uuid'] = str(self.uploaded_by.uuid) if self.uploaded_by else None
        return data

    def __str__(self):
        return f"{self.title} ({self.document_type})"


class LeaveType(SyncMixin, models.Model):
    name = models.CharField(max_length=100)
    short_name = models.CharField(max_length=20, blank=True, default='')
    is_paid = models.BooleanField(default=True)
    annual_quota = models.PositiveIntegerField(default=0)
    max_carryover = models.PositiveIntegerField(default=0)
    requires_approval = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class LeaveRequest(SyncMixin, models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        CANCELED = 'CANCELED', 'Canceled'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_requests')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE, related_name='requests')
    start_date = models.DateField()
    end_date = models.DateField()
    days_count = models.DecimalField(max_digits=5, decimal_places=1)
    reason = models.TextField(blank=True, default='')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    approved_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_leaves',
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-start_date']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        data['leave_type_uuid'] = str(self.leave_type.uuid) if self.leave_type else None
        data['approved_by_uuid'] = str(self.approved_by.uuid) if self.approved_by else None
        return data

    def __str__(self):
        return f"{self.employee} - {self.leave_type} ({self.start_date} to {self.end_date})"


class LeaveBalance(SyncMixin, models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_balances')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE, related_name='balances')
    year = models.PositiveIntegerField()
    allocated_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    used_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    carried_over = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        unique_together = ['employee', 'leave_type', 'year']
        ordering = ['-year']

    @property
    def remaining_days(self):
        return self.allocated_days + self.carried_over - self.used_days

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        data['leave_type_uuid'] = str(self.leave_type.uuid) if self.leave_type else None
        return data

    def __str__(self):
        return f"{self.employee} - {self.leave_type} ({self.year}): {self.remaining_days}d remaining"


class Attendance(SyncMixin, models.Model):
    class Status(models.TextChoices):
        PRESENT = 'PRESENT', 'Present'
        ABSENT = 'ABSENT', 'Absent'
        LATE = 'LATE', 'Late'
        HALF_DAY = 'HALF_DAY', 'Half Day'
        ON_LEAVE = 'ON_LEAVE', 'On Leave'

    class Source(models.TextChoices):
        MANUAL = 'MANUAL', 'Manual'
        AUTO_POS = 'AUTO_POS', 'Auto POS'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='attendances')
    date = models.DateField()
    check_in = models.DateTimeField(null=True, blank=True)
    check_out = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PRESENT)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    work_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    overtime_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        unique_together = ['employee', 'date']
        ordering = ['-date']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        return data

    def __str__(self):
        return f"{self.employee} - {self.date} ({self.status})"


class EmployeeDocument(SyncMixin, models.Model):
    class DocumentType(models.TextChoices):
        ID_CARD = 'ID_CARD', 'ID Card'
        PASSPORT = 'PASSPORT', 'Passport'
        CONTRACT = 'CONTRACT', 'Contract'
        CERTIFICATE = 'CERTIFICATE', 'Certificate'
        MEDICAL = 'MEDICAL', 'Medical'
        OTHER = 'OTHER', 'Other'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='documents')
    document_type = models.CharField(max_length=12, choices=DocumentType.choices, default=DocumentType.OTHER)
    title = models.CharField(max_length=200)
    file_url = models.URLField(
        max_length=500, blank=True, default='',
        help_text='DEPRECATED: legacy URL. New uploads should use file.',
    )
    file = models.FileField(
        upload_to='hr/employee_documents/%Y/%m/', blank=True, null=True,
        help_text='Private employee document (passport/ID/etc). Served via auth-gated download endpoint only.',
    )
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='verified_documents',
    )
    notes = models.TextField(blank=True, default='')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-uploaded_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        data['verified_by_uuid'] = str(self.verified_by.uuid) if self.verified_by else None
        return data

    def __str__(self):
        return f"{self.employee} - {self.title} ({self.document_type})"


class PerformanceReview(SyncMixin, models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        SUBMITTED = 'SUBMITTED', 'Submitted'
        ACKNOWLEDGED = 'ACKNOWLEDGED', 'Acknowledged'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='performance_reviews')
    reviewer = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, related_name='given_reviews',
    )
    review_period_start = models.DateField()
    review_period_end = models.DateField()
    rating = models.PositiveSmallIntegerField(default=3)
    strengths = models.TextField(blank=True, default='')
    improvements = models.TextField(blank=True, default='')
    goals = models.TextField(blank=True, default='')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-review_period_end']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        data['reviewer_uuid'] = str(self.reviewer.uuid) if self.reviewer else None
        return data

    def __str__(self):
        return f"Review: {self.employee} ({self.review_period_start} to {self.review_period_end})"


class PerformanceGoal(SyncMixin, models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
        COMPLETED = 'COMPLETED', 'Completed'
        CANCELED = 'CANCELED', 'Canceled'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='performance_goals')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    target_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    created_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='created_goals',
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-target_date']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        return data

    def __str__(self):
        return f"Goal: {self.title} - {self.employee}"


class EmploymentEvent(SyncMixin, models.Model):
    class EventType(models.TextChoices):
        HIRED = 'HIRED', 'Hired'
        PROMOTED = 'PROMOTED', 'Promoted'
        TRANSFERRED = 'TRANSFERRED', 'Transferred'
        CONTRACT_RENEWED = 'CONTRACT_RENEWED', 'Contract Renewed'
        CONTRACT_TERMINATED = 'CONTRACT_TERMINATED', 'Contract Terminated'
        WARNING = 'WARNING', 'Warning'
        SALARY_CHANGE = 'SALARY_CHANGE', 'Salary Change'
        SUSPENDED = 'SUSPENDED', 'Suspended'
        REINSTATED = 'REINSTATED', 'Reinstated'
        RESIGNED = 'RESIGNED', 'Resigned'
        TERMINATED = 'TERMINATED', 'Terminated'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='employment_events')
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    event_date = models.DateField()
    description = models.TextField(blank=True, default='')
    old_value = models.CharField(max_length=255, blank=True, default='')
    new_value = models.CharField(max_length=255, blank=True, default='')
    created_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='created_events',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-event_date', '-created_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        return data

    def __str__(self):
        return f"{self.employee} - {self.event_type} ({self.event_date})"
