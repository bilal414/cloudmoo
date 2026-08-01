from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apps', '0080_assetmonitoringstate_and_email_delivery'),
    ]

    operations = [
        migrations.AddField(
            model_name='assetmonitoringstate',
            name='check_generation',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='assetmonitoringstate',
            name='check_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
