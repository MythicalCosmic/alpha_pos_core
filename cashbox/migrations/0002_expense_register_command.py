from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0042_inkassa_register_commands'),
        ('cashbox', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='cashboxexpense',
            name='register_command',
            field=models.BooleanField(default=False),
        ),
    ]
