from django.db import migrations, models


class Migration(migrations.Migration):
    """Enforce the License singleton at the DB layer.

    `License.save()` already pins `pk=1`, but bulk_create / raw SQL /
    `objects.create(id=2)` would bypass it. A CHECK constraint makes
    the singleton invariant load-bearing for every writer.
    """

    dependencies = [
        ('licensing', '0001_initial'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='license',
            constraint=models.CheckConstraint(
                condition=models.Q(id=1),
                name='license_singleton_pk1',
            ),
        ),
    ]
