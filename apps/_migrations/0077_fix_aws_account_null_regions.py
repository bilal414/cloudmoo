# Generated manually to fix NULL region values

from django.db import migrations


def fix_null_regions(apps, schema_editor):
    """
    Fix any AWS accounts that have NULL region values by setting them to us-east-1
    """
    CoreAWSAccount = apps.get_model('apps', 'CoreAWSAccount')
    
    # Update any records with NULL or empty region to default us-east-1
    CoreAWSAccount.objects.filter(region__isnull=True).update(region='us-east-1')
    CoreAWSAccount.objects.filter(region='').update(region='us-east-1')


def reverse_fix_null_regions(apps, schema_editor):
    """
    Reverse operation - no action needed as we don't want to set regions back to NULL
    """
    pass


class Migration(migrations.Migration):
    
    dependencies = [
        ('apps', '0076_remove_coreawsacmcertificate_aws_eventbridge_rule_arn_and_more'),
    ]
    
    operations = [
        migrations.RunPython(fix_null_regions, reverse_fix_null_regions),
    ] 