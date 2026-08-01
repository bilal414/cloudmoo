from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('apps', '0083_status_email_delivery_index'),
    ]

    operations = [
        migrations.RenameIndex(
            model_name='assetstatusemail',
            old_name='asset_status_email_delivery_idx',
            new_name='asset_email_delivery_idx',
        ),
    ]
