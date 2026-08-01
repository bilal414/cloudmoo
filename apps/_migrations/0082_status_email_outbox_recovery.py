from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apps', '0081_monitoringcheckgeneration'),
    ]

    operations = [
        migrations.AddField(
            model_name='assetstatusemail',
            name='asset_content_type_id',
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='assetstatusemail',
            name='last_attempt_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
