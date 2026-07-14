from django.db import migrations, models
from django.db.models import Count, Q


DEDUPE_NOTE = '[automatic repair: duplicate active shift abandoned]'


def abandon_duplicate_live_shifts(apps, schema_editor):
    """Keep the newest live shift and close older duplicates without posting.

    Duplicate live rows have ambiguous drawer ownership. The newest start is
    the safest shift to keep operational; every older row is closed exactly at
    that boundary and marked ABANDONED so no reconciliation/treasury movement
    is invented by the migration. Historical rows remain available for audit.
    """
    Shift = apps.get_model('base', 'Shift')
    duplicate_users = (
        Shift.objects.filter(
            is_deleted=False,
            status='ACTIVE',
            end_time__isnull=True,
        )
        .values('user_id')
        .annotate(row_count=Count('id'))
        .filter(row_count__gt=1)
    )
    for group in duplicate_users.iterator(chunk_size=500):
        shifts = list(
            Shift.objects.filter(
                user_id=group['user_id'],
                is_deleted=False,
                status='ACTIVE',
                end_time__isnull=True,
            ).order_by('-start_time', '-pk')
        )
        keeper = shifts[0]
        for duplicate in shifts[1:]:
            notes = str(duplicate.notes or '').strip()
            if DEDUPE_NOTE not in notes:
                notes = f'{notes}\n{DEDUPE_NOTE}'.strip()
            boundary = max(duplicate.start_time, keeper.start_time)
            Shift.objects.filter(pk=duplicate.pk).update(
                status='ABANDONED',
                end_time=boundary,
                notes=notes,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0043_order_refund_ledger'),
    ]

    operations = [
        migrations.RunPython(
            abandon_duplicate_live_shifts,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='shift',
            constraint=models.UniqueConstraint(
                fields=('user',),
                condition=(
                    Q(is_deleted=False)
                    & Q(status='ACTIVE')
                    & Q(end_time__isnull=True)
                ),
                name='uniq_live_active_shift_per_user',
            ),
        ),
    ]
