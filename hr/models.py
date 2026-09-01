import re

from django.core.exceptions import ValidationError
from django.db import models
from base.financial import FinancialReportingGroup
from base.models import SyncMixin, SyncManager


def _default_expense_sources():
    return ['DRAWER', 'SAFE', 'BANK']


class Department(SyncMixin, models.Model):
    SYNC_PULL_SCOPE = 'global'
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
    SYNC_PULL_SCOPE = 'global'
    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    budget_limit = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    allowed_sources = models.JSONField(
        default=_default_expense_sources, blank=True,
    )
    requires_receipt = models.BooleanField(default=False)
    requires_description = models.BooleanField(default=False)
    reporting_group = models.CharField(
        max_length=32,
        choices=FinancialReportingGroup.choices,
        default=FinancialReportingGroup.REVIEW,
        db_index=True,
    )
    created_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_expense_categories',
    )
    updated_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='updated_expense_categories',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        verbose_name_plural = 'expense categories'
        ordering = ['sort_order', 'name', 'id']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        data['updated_by_uuid'] = str(self.updated_by.uuid) if self.updated_by else None
        return data

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values_list(
                'code', flat=True,
            ).first()
            if original is not None and self.code != original:
                raise ValidationError({'code': 'Code is immutable.'})
        elif not str(self.code or '').strip():
            stem = re.sub(
                r'[^A-Z0-9]+', '_', str(self.name or '').upper(),
            ).strip('_') or 'EXPENSE'
            stem = stem[:55]
            candidate = stem
            if type(self).objects.filter(code=candidate).exists():
                candidate = f'{stem}_{self.uuid.hex[:8].upper()}'
            self.code = candidate
        return super().save(*args, **kwargs)

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
        CANCELED = 'CANCELED', 'Canceled'
        VOIDED = 'VOIDED', 'Voided'

    class Source(models.TextChoices):
        DRAWER = 'DRAWER', 'Shift drawer'
        SAFE = 'SAFE', 'Safe'
        BANK = 'BANK', 'Bank'

    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expenses',
    )
    category_code_snapshot = models.CharField(max_length=64, blank=True, default='')
    category_name_snapshot = models.CharField(max_length=100, blank=True, default='')
    category_allowed_sources_snapshot = models.JSONField(default=list, blank=True)
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
    requested_source = models.CharField(
        max_length=10, choices=Source.choices, blank=True, default='',
    )
    shift = models.ForeignKey(
        'base.Shift', on_delete=models.PROTECT, null=True, blank=True,
        related_name='expense_requests',
    )
    subject_user = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='expense_requests_as_subject',
    )
    fee_uzs = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    fee_percent = models.DecimalField(
        max_digits=7, decimal_places=4, null=True, blank=True,
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
    canceled_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='canceled_expenses',
    )
    voided_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='voided_expenses',
    )
    treasury_transaction = models.OneToOneField(
        'base.TreasuryTransaction', on_delete=models.PROTECT,
        null=True, blank=True, related_name='expense_payment',
    )
    treasury_reversal = models.OneToOneField(
        'base.TreasuryTransaction', on_delete=models.PROTECT,
        null=True, blank=True, related_name='expense_void',
    )
    payment_action_id = models.UUIDField(null=True, blank=True, unique=True)
    void_action_id = models.UUIDField(null=True, blank=True, unique=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True, db_index=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True, default='')
    void_reason = models.TextField(blank=True, default='')
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-expense_date', '-created_at']
        indexes = [
            models.Index(fields=['branch_id', 'status', 'expense_date']),
            models.Index(fields=['branch_id', 'category', 'paid_at']),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['expense_category_uuid'] = str(self.category.uuid) if self.category else None
        data['shift_uuid'] = str(self.shift.uuid) if self.shift else None
        data['subject_user_uuid'] = (
            str(self.subject_user.uuid) if self.subject_user else None
        )
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        data['approved_by_uuid'] = str(self.approved_by.uuid) if self.approved_by else None
        data['paid_by_uuid'] = str(self.paid_by.uuid) if self.paid_by else None
        data['canceled_by_uuid'] = str(self.canceled_by.uuid) if self.canceled_by else None
        data['voided_by_uuid'] = str(self.voided_by.uuid) if self.voided_by else None
        return data

    def __str__(self):
        return f"Expense #{self.id} - {self.amount} ({self.status})"


class ExpenseTransition(SyncMixin, models.Model):
    expense = models.ForeignKey(
        Expense, on_delete=models.PROTECT, related_name='transitions',
    )
    previous_status = models.CharField(max_length=10, blank=True, default='')
    new_status = models.CharField(max_length=10)
    actor = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='expense_transitions',
    )
    actor_display_snapshot = models.CharField(max_length=200, blank=True, default='')
    reason = models.TextField(blank=True, default='')
    idempotency_key = models.CharField(max_length=128, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()
    _sync_append_only = True

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [models.Index(fields=['expense', 'created_at'])]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['expense_uuid'] = str(self.expense.uuid)
        data['actor_uuid'] = str(self.actor.uuid) if self.actor else None
        return data

    def save(self, *args, **kwargs):
        if self.pk:
            raise TypeError('ExpenseTransition is append-only and cannot be updated')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError('ExpenseTransition is append-only and cannot be deleted')

    def hard_delete(self, *args, **kwargs):
        raise TypeError('ExpenseTransition is append-only and cannot be deleted')


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
    SYNC_PULL_SCOPE = 'global'
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
    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)
    grace_minutes = models.PositiveIntegerField(default=0)
    worked_minutes = models.PositiveIntegerField(default=0)
    scheduled_minutes = models.PositiveIntegerField(default=0)
    overtime_minutes = models.PositiveIntegerField(default=0)
    late_minutes = models.PositiveIntegerField(default=0)
    early_leave_minutes = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_attendance_records',
    )
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
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        return data

    def __str__(self):
        return f"{self.employee} - {self.date} ({self.status})"


class EmployeeWorkSchedule(SyncMixin, models.Model):
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='work_schedules',
    )
    weekday = models.PositiveSmallIntegerField()
    scheduled_start_local = models.TimeField()
    scheduled_end_local = models.TimeField()
    is_overnight = models.BooleanField(default=False)
    grace_minutes = models.PositiveIntegerField(default=0)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, related_name='created_work_schedules',
    )
    updated_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, related_name='updated_work_schedules',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['employee_id', 'weekday', '-effective_from']
        indexes = [models.Index(fields=['employee', 'weekday', 'effective_from', 'effective_to'])]
        constraints = [
            models.CheckConstraint(condition=models.Q(weekday__gte=0, weekday__lte=6), name='schedule_weekday_0_6'),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=models.F('effective_from')),
                name='schedule_effective_range_valid',
            ),
            models.UniqueConstraint(
                fields=['employee', 'weekday', 'effective_from'],
                name='uniq_employee_schedule_effective_start',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        data['updated_by_uuid'] = str(self.updated_by.uuid) if self.updated_by else None
        return data


class AttendanceAdjustmentRequest(SyncMixin, models.Model):
    class ReasonCategory(models.TextChoices):
        MISSING_ENTRY = 'MISSING_ENTRY', 'Missing entry'
        DEVICE_FAILURE = 'DEVICE_FAILURE', 'Device failure'
        MANAGER_INSTRUCTION = 'MANAGER_INSTRUCTION', 'Manager instruction'
        DATA_ENTRY_ERROR = 'DATA_ENTRY_ERROR', 'Data entry error'
        OTHER = 'OTHER', 'Other'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        CANCELLED = 'CANCELLED', 'Cancelled'

    attendance = models.ForeignKey(Attendance, on_delete=models.PROTECT, related_name='adjustment_requests')
    original_check_in = models.DateTimeField(null=True, blank=True)
    original_check_out = models.DateTimeField(null=True, blank=True)
    requested_check_in = models.DateTimeField(null=True, blank=True)
    requested_check_out = models.DateTimeField(null=True, blank=True)
    reason_category = models.CharField(max_length=24, choices=ReasonCategory.choices)
    reason_text = models.TextField(blank=True, default='')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, related_name='attendance_adjustment_requests',
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, null=True, blank=True,
        related_name='reviewed_attendance_adjustments',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-requested_at']
        constraints = [
            models.UniqueConstraint(
                fields=['attendance'], condition=models.Q(status='PENDING'),
                name='uniq_pending_attendance_adjustment',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['attendance_uuid'] = str(self.attendance.uuid) if self.attendance else None
        data['requested_by_uuid'] = str(self.requested_by.uuid) if self.requested_by else None
        data['reviewed_by_uuid'] = str(self.reviewed_by.uuid) if self.reviewed_by else None
        return data


class AttendanceExcuse(SyncMixin, models.Model):
    class Category(models.TextChoices):
        MEDICAL = 'MEDICAL', 'Medical'
        FAMILY = 'FAMILY', 'Family'
        TRANSPORT = 'TRANSPORT', 'Transport'
        APPROVED_LEAVE = 'APPROVED_LEAVE', 'Approved leave'
        MANAGER_INSTRUCTION = 'MANAGER_INSTRUCTION', 'Manager instruction'
        OTHER = 'OTHER', 'Other'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    attendance = models.ForeignKey(Attendance, on_delete=models.PROTECT, related_name='excuses')
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='attendance_excuses')
    submitted_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, related_name='submitted_attendance_excuses',
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    category = models.CharField(max_length=24, choices=Category.choices)
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    excused_late_minutes = models.PositiveIntegerField(default=0)
    excused_early_leave_minutes = models.PositiveIntegerField(default=0)
    excused_absence_minutes = models.PositiveIntegerField(default=0)
    reviewer = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, null=True, blank=True,
        related_name='reviewed_attendance_excuses',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-submitted_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['attendance_uuid'] = str(self.attendance.uuid) if self.attendance else None
        data['employee_uuid'] = str(self.employee.uuid) if self.employee else None
        data['submitted_by_uuid'] = str(self.submitted_by.uuid) if self.submitted_by else None
        data['reviewer_uuid'] = str(self.reviewer.uuid) if self.reviewer else None
        return data


class DisciplinaryRule(models.Model):
    class Category(models.TextChoices):
        ATTENDANCE = 'ATTENDANCE', 'Attendance'
        CONDUCT = 'CONDUCT', 'Conduct'
        QUALITY = 'QUALITY', 'Quality'
        PREPARATION_TIME = 'PREPARATION_TIME', 'Preparation time'
        OTHER = 'OTHER', 'Other'

    code = models.CharField(max_length=50, unique=True)
    category = models.CharField(max_length=24, choices=Category.choices)
    title = models.CharField(max_length=200)
    description = models.TextField()
    default_amount_uzs = models.PositiveBigIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    requires_evidence = models.BooleanField(default=True)
    requires_comment = models.BooleanField(default=True)
    created_by = models.ForeignKey('base.User', on_delete=models.PROTECT, related_name='+')
    updated_by = models.ForeignKey('base.User', on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'code']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=models.F('effective_from')),
                name='discipline_rule_effective_range_valid',
            ),
        ]


class PreparationAuditCategory(models.Model):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['code']


class PreparationAudit(models.Model):
    class PerformanceStatus(models.TextChoices):
        ON_TIME = 'ON_TIME', 'On time'
        SLIGHTLY_LATE = 'SLIGHTLY_LATE', 'Slightly late'
        VERY_LATE = 'VERY_LATE', 'Very late'
        UNTRACKED = 'UNTRACKED', 'Untracked'

    class ReviewStatus(models.TextChoices):
        NOT_REQUIRED = 'NOT_REQUIRED', 'Not required'
        PENDING = 'PENDING', 'Pending'
        COMPLETED = 'COMPLETED', 'Completed'
        EXCUSED = 'EXCUSED', 'Excused'

    order = models.OneToOneField('base.Order', on_delete=models.PROTECT, related_name='preparation_audit')
    branch_id = models.CharField(max_length=50, blank=True, default='', db_index=True)
    created_at_snapshot = models.DateTimeField()
    ready_at_snapshot = models.DateTimeField()
    elapsed_seconds = models.PositiveIntegerField()
    target_seconds = models.PositiveIntegerField(null=True, blank=True)
    target_name_snapshot = models.CharField(max_length=100, blank=True, default='')
    performance_status = models.CharField(max_length=20, choices=PerformanceStatus.choices)
    review_required = models.BooleanField(default=False)
    review_status = models.CharField(max_length=16, choices=ReviewStatus.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewer = models.ForeignKey('base.User', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reopened_by = models.ForeignKey('base.User', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopen_reason = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-ready_at_snapshot']
        indexes = [
            models.Index(fields=['branch_id', 'ready_at_snapshot']),
            models.Index(fields=['performance_status', 'review_status']),
        ]


class DisciplinaryCase(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        SUBMITTED = 'SUBMITTED', 'Submitted'
        EXCUSED = 'EXCUSED', 'Excused'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        VOIDED = 'VOIDED', 'Voided'
        APPROVED_PENDING_PAYROLL = 'APPROVED_PENDING_PAYROLL', 'Approved pending payroll'

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='disciplinary_cases')
    occurred_at = models.DateTimeField()
    business_date = models.DateField(db_index=True)
    rule = models.ForeignKey(DisciplinaryRule, on_delete=models.PROTECT, related_name='cases')
    rule_code_snapshot = models.CharField(max_length=50)
    rule_title_snapshot = models.CharField(max_length=200)
    rule_category_snapshot = models.CharField(max_length=24)
    rule_amount_snapshot_uzs = models.PositiveBigIntegerField(default=0)
    amount_uzs = models.PositiveBigIntegerField(default=0)
    evidence = models.TextField(blank=True, default='')
    comment = models.TextField(blank=True, default='')
    attendance = models.ForeignKey(Attendance, on_delete=models.PROTECT, null=True, blank=True, related_name='disciplinary_cases')
    attendance_excuse = models.ForeignKey(AttendanceExcuse, on_delete=models.PROTECT, null=True, blank=True, related_name='disciplinary_cases')
    preparation_audit = models.ForeignKey(PreparationAudit, on_delete=models.PROTECT, null=True, blank=True, related_name='disciplinary_cases')
    excuse_text = models.TextField(blank=True, default='')
    excuse_review_state = models.CharField(max_length=10, blank=True, default='')
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey('base.User', on_delete=models.PROTECT, related_name='created_disciplinary_cases')
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey('base.User', on_delete=models.PROTECT, null=True, blank=True, related_name='reviewed_disciplinary_cases')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default='')
    void_reason = models.TextField(blank=True, default='')
    payroll_period_year = models.PositiveIntegerField(null=True, blank=True)
    payroll_period_month = models.PositiveSmallIntegerField(null=True, blank=True)
    salary_deduction = models.OneToOneField(SalaryDeduction, on_delete=models.PROTECT, null=True, blank=True, related_name='disciplinary_case')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-business_date', '-created_at']
        indexes = [models.Index(fields=['employee', 'business_date', 'status'])]


class PreparationAuditReview(models.Model):
    preparation_audit = models.ForeignKey(PreparationAudit, on_delete=models.PROTECT, related_name='reviews')
    category = models.ForeignKey(PreparationAuditCategory, on_delete=models.PROTECT, related_name='reviews')
    comment = models.TextField()
    responsible_employee = models.ForeignKey(Employee, on_delete=models.PROTECT, null=True, blank=True, related_name='preparation_audit_reviews')
    linked_disciplinary_case = models.ForeignKey(DisciplinaryCase, on_delete=models.PROTECT, null=True, blank=True, related_name='preparation_reviews')
    reviewed_by = models.ForeignKey('base.User', on_delete=models.PROTECT, related_name='preparation_audit_reviews')
    reviewed_at = models.DateTimeField(auto_now_add=True)
    is_current = models.BooleanField(default=True)
    reopened_by = models.ForeignKey('base.User', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopen_reason = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-reviewed_at']
        constraints = [
            models.UniqueConstraint(
                fields=['preparation_audit'], condition=models.Q(is_current=True),
                name='uniq_current_preparation_audit_review',
            ),
        ]


class PreparationAuditPeriodClose(models.Model):
    branch_id = models.CharField(max_length=50, blank=True, default='', db_index=True)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    closed_by = models.ForeignKey('base.User', on_delete=models.PROTECT, related_name='+')
    closed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['branch_id', 'period_start', 'period_end'],
                name='uniq_preparation_audit_period_close',
            ),
        ]


class OperationalAuditEvent(models.Model):
    entity_type = models.CharField(max_length=60, db_index=True)
    entity_id = models.PositiveBigIntegerField(db_index=True)
    action = models.CharField(max_length=40)
    actor = models.ForeignKey('base.User', on_delete=models.PROTECT, related_name='+')
    occurred_at = models.DateTimeField(auto_now_add=True)
    previous_state = models.CharField(max_length=40, blank=True, default='')
    new_state = models.CharField(max_length=40, blank=True, default='')
    reason = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['occurred_at', 'id']
        indexes = [models.Index(fields=['entity_type', 'entity_id', 'occurred_at'])]


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
