from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0010_notificationchat'),
    ]

    operations = [
        migrations.AlterField(
            model_name='notificationlog',
            name='recipient',
            field=models.TextField(),
        ),
    ]
