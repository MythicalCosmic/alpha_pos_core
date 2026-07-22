from django.db import migrations, models


class Migration(migrations.Migration):
    """Add a rolling-upgrade-safe exclusive cashier slot per POS install.

    Existing rows receive the blank default. We intentionally do not guess a
    device for legacy ACTIVE shifts because neither the database nor ephemeral
    presence cache is durable evidence of which till opened them. Blank values
    are outside the constraint, so those shifts can finish normally; the first
    cashier shift opened by upgraded code records DEVICE_ID and is protected.
    """

    dependencies = [
        ('base', '0053_payment_action_identity'),
    ]

    operations = [
        migrations.AddField(
            model_name='shift',
            name='device_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddConstraint(
            model_name='shift',
            constraint=models.UniqueConstraint(
                condition=(
                    models.Q(is_deleted=False)
                    & models.Q(status='ACTIVE')
                    & models.Q(end_time__isnull=True)
                    & ~models.Q(device_id='')
                ),
                fields=('device_id',),
                name='uniq_live_shift_per_device',
            ),
        ),
    ]
