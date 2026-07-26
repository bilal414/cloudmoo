import uuid
from django.db import migrations, models


def generate_uuid_for_existing_clouds(apps, schema_editor):
    """
    Generate a UUID for each existing CoreCloud record
    """
    CoreCloud = apps.get_model('apps', 'CoreCloud')
    for cloud in CoreCloud.objects.all():
        cloud.uuid = uuid.uuid4()
        cloud.save(update_fields=['uuid'])


class Migration(migrations.Migration):
    dependencies = [
        ('apps', '0057_coreupcloudaccount_coreupcloudserver_and_more'),
    ]

    operations = [
        # First add the field allowing NULL (required for existing rows)
        migrations.AddField(
            model_name='corecloud',
            name='uuid',
            field=models.UUIDField(null=True, blank=True),
        ),

        # Run the function to populate UUIDs for existing records
        migrations.RunPython(generate_uuid_for_existing_clouds),

        # Now make the field non-nullable and set the default for future records
        migrations.AlterField(
            model_name='corecloud',
            name='uuid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
