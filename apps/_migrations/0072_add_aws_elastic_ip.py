# Generated on AWS Elastic IP implementation

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('apps', '0071_add_aws_snapshot'),
    ]

    operations = [
        migrations.CreateModel(
            name='CoreAWSElasticIP',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('modified', models.DateTimeField(auto_now=True)),
                ('unique_id', models.CharField(max_length=255)),
                ('name', models.CharField(max_length=255)),
                ('type', models.CharField(choices=[('server', 'Server'), ('volume', 'Volume'), ('database', 'Database'), ('rds_database', 'RDS Database'), ('lambda', 'Lambda Function'), ('dynamodb', 'DynamoDB Table'), ('s3_bucket', 'S3 Bucket'), ('acm_certificate', 'ACM Certificate'), ('snapshot', 'Snapshot'), ('elastic_ip', 'Elastic IP')], default='elastic_ip', max_length=50)),
                ('monitoring', models.CharField(choices=[('active', 'Active'), ('disabled', 'Disabled'), ('no_longer_exists', 'No Longer Exists')], default='active', max_length=50)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('notification_emails', models.JSONField(default=list)),
                ('aws_eventbridge_rule_arn', models.CharField(blank=True, max_length=255, null=True)),
                ('aws_schedule_arn', models.CharField(blank=True, max_length=255, null=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='elastic_ips', to='apps.coreawsaccount')),
            ],
            options={
                'db_table': 'core_aws_elastic_ip',
            },
        ),
    ] 