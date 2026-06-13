from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hr', '0003_employee_medical_book_expiry_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='expense',
            name='receipt_file',
            field=models.FileField(
                blank=True,
                help_text='Private receipt file. Served via auth-gated download endpoint only.',
                null=True,
                upload_to='hr/expenses/%Y/%m/',
            ),
        ),
        migrations.AlterField(
            model_name='expense',
            name='receipt_image_url',
            field=models.URLField(
                blank=True,
                default='',
                help_text='DEPRECATED: legacy URL. New uploads should use receipt_file.',
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name='contractdocument',
            name='file',
            field=models.FileField(
                blank=True,
                help_text='Private contract document. Served via auth-gated download endpoint only.',
                null=True,
                upload_to='hr/contracts/%Y/%m/',
            ),
        ),
        migrations.AlterField(
            model_name='contractdocument',
            name='file_url',
            field=models.URLField(
                blank=True,
                default='',
                help_text='DEPRECATED: legacy URL. New uploads should use file.',
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name='employeedocument',
            name='file',
            field=models.FileField(
                blank=True,
                help_text='Private employee document (passport/ID/etc). Served via auth-gated download endpoint only.',
                null=True,
                upload_to='hr/employee_documents/%Y/%m/',
            ),
        ),
        migrations.AlterField(
            model_name='employeedocument',
            name='file_url',
            field=models.URLField(
                blank=True,
                default='',
                help_text='DEPRECATED: legacy URL. New uploads should use file.',
                max_length=500,
            ),
        ),
    ]
