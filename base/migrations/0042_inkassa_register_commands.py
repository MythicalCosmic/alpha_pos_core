from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
from django.db.models import Count, Max, Min, Q, Sum


COMMAND_MARKER = '[ALPHAPOS_REGISTER_COMMAND_V1]'
EXPENSE_COMMAND_MARKER = '[ALPHAPOS_CASHBOX_COMMAND_V1]'


def normalize_legacy_multi_tender_batches(apps, schema_editor):
    """Remove historical per-method copies of one period aggregate.

    Old mixed inkassa wrote the same order count/revenue onto every tender row.
    Reports and the AI then summed those rows and multiplied the period. Rows
    from one call share an exact non-null (branch_id, period_start), while a
    later call starts at the previous row's period_end. Only groups whose
    aggregate values are identical are safe to normalize automatically.
    """
    Inkassa = apps.get_model('base', 'Inkassa')
    groups = (
        Inkassa.objects.filter(is_deleted=False, period_start__isnull=False)
        .values('branch_id', 'period_start')
        .annotate(
            row_count=Count('id'),
            min_orders=Min('total_orders'),
            max_orders=Max('total_orders'),
            min_revenue=Min('total_revenue'),
            max_revenue=Max('total_revenue'),
        )
        .filter(row_count__gt=1)
    )
    for group in groups.iterator(chunk_size=500):
        if (
            group['min_orders'] != group['max_orders']
            or group['min_revenue'] != group['max_revenue']
        ):
            continue
        rows = Inkassa.objects.filter(
            is_deleted=False,
            branch_id=group['branch_id'],
            period_start=group['period_start'],
        ).order_by('created_at', 'pk')
        keeper = rows.values_list('pk', flat=True).first()
        if keeper is not None:
            rows.exclude(pk=keeper).update(total_orders=0, total_revenue=0)


def apply_transition_commands(apps, schema_editor):
    """Apply commands pulled by an old desktop before it was upgraded.

    ``register_command`` did not exist in the old client, but the compatible
    marker was stored in ``notes``. Historical inkassas have neither marker nor
    flag and are therefore never replayed. Cloud keeps the branch-reported
    balance untouched; only the owning local database applies commands.
    """
    if getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud':
        return

    CashRegister = apps.get_model('base', 'CashRegister')
    Inkassa = apps.get_model('base', 'Inkassa')
    own_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
    if not own_branch:
        return

    command_total = (
        Inkassa.objects.filter(
            branch_id=own_branch,
            is_deleted=False,
            inkass_type='CASH',
        )
        .filter(
            Q(register_command=True) | Q(notes__startswith=COMMAND_MARKER)
        )
        .aggregate(total=Sum('amount'))['total']
        or Decimal('0')
    )
    # Cashbox 0001 already exists (declared as a migration dependency below),
    # while its new flag is added by cashbox 0002 after this transition step.
    # The marker therefore bridges expenses pulled by an older desktop.
    CashboxExpense = apps.get_model('cashbox', 'CashboxExpense')
    command_total += (
        CashboxExpense.objects.filter(
            branch_id=own_branch,
            is_deleted=False,
            comment__startswith=EXPENSE_COMMAND_MARKER,
        ).aggregate(total=Sum('amount'))['total']
        or Decimal('0')
    )
    register = CashRegister.objects.filter(
        branch_id=own_branch, is_deleted=False,
    ).first()
    if register is None or command_total <= (register.remote_cash_out_applied_total or 0):
        return

    delta = command_total - (register.remote_cash_out_applied_total or Decimal('0'))
    if delta > (register.current_balance or Decimal('0')):
        # A transition command may have arrived before upgrade while later
        # local spending consumed the reported cash. Never make the physical
        # drawer negative or falsely acknowledge an unapplied command.
        return
    CashRegister.objects.filter(pk=register.pk).update(
        current_balance=(register.current_balance or Decimal('0')) - delta,
        remote_cash_out_applied_total=command_total,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0041_unique_active_treasury_account'),
        ('cashbox', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='cashregister',
            name='remote_cash_out_applied_total',
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=18,
            ),
        ),
        migrations.AddField(
            model_name='inkassa',
            name='register_command',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(
            normalize_legacy_multi_tender_batches,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(
            apply_transition_commands,
            migrations.RunPython.noop,
        ),
    ]
