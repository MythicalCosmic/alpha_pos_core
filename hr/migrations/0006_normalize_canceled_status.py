from django.db import migrations


def forwards(apps, schema_editor):
    # Mirror of stock/0003 and base/0008 for the HR side. LeaveRequest and
    # PerformanceGoal were storing status='CANCELLED' while the enums have
    # been normalized to 'CANCELED' to match base.Order.
    for label in ('LeaveRequest', 'PerformanceGoal'):
        Model = apps.get_model('hr', label)
        Model.objects.filter(status='CANCELLED').update(status='CANCELED')


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('hr', '0005_alter_leaverequest_status_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
