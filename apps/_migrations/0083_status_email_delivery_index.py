from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apps', '0082_status_email_outbox_recovery'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='assetstatusemail',
            index=models.Index(
                fields=['delivery_status', 'last_attempt_at'],
                name='asset_status_email_delivery_idx',
            ),
        ),
    ]
