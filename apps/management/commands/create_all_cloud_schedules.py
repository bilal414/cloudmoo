from django.core.management.base import BaseCommand
from apps.console.cloud.models import CoreCloud
import time


class Command(BaseCommand):
    help = 'Create AWS EventBridge schedules for active clouds and monitored assets'

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Confirmation flag to proceed with creation',
        )
        parser.add_argument(
            '--cloud-id',
            type=int,
            help='Specific cloud ID to target (optional)',
        )

    def handle(self, *args, **options):
        confirm = options.get('confirm')
        cloud_id = options.get('cloud_id')

        if not confirm:
            self.stdout.write(self.style.WARNING(
                'This command will create AWS EventBridge schedules for all active clouds and monitored assets.\n'
                'To confirm, run the command with --confirm flag.'
            ))
            return

        # Get active clouds or a specific cloud
        if cloud_id:
            clouds = CoreCloud.objects.filter(id=cloud_id)
            self.stdout.write(self.style.NOTICE(f'Processing only cloud ID: {cloud_id}'))
        else:
            clouds = CoreCloud.objects.filter(status=CoreCloud.Status.ACTIVE)
            self.stdout.write(self.style.NOTICE(f'Processing {clouds.count()} active clouds'))

        # Track creation statistics
        cloud_schedules_created = 0
        clouds_processed = 0

        # Process each cloud
        for cloud in clouds:
            try:
                self.stdout.write(f'Processing cloud: {cloud.name} (ID: {cloud.id})')

                # Only create schedules for ACTIVE clouds
                if cloud.status == CoreCloud.Status.ACTIVE:
                    # Create cloud schedule if it doesn't exist
                    if not cloud.aws_schedule_arn:
                        self.stdout.write(f'  Creating cloud schedule for cloud {cloud.id}...')
                        cloud.aws_schedule_create()
                        cloud_schedules_created += 1
                        self.stdout.write(self.style.SUCCESS(f'  Created cloud schedule for {cloud.name}'))
                    else:
                        self.stdout.write(f'  Cloud {cloud.id} already has a schedule')
                    # Create schedules for all active assets
                    self.stdout.write(f'  Creating asset schedules for cloud {cloud.id}...')
                    cloud.create_all_asset_schedules()
                    self.stdout.write(self.style.SUCCESS(f'  Created asset schedules for cloud {cloud.name}'))
                else:
                    self.stdout.write(self.style.WARNING(f'  Skipping cloud {cloud.id} - not in ACTIVE status'))

                clouds_processed += 1

                # Small delay to prevent API throttling
                time.sleep(0.5)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error processing cloud {cloud.id}: {str(e)}'))

        # Print summary
        self.stdout.write(self.style.SUCCESS(
            f'\nSchedule creation completed:\n'
            f'- Clouds processed: {clouds_processed}\n'
            f'- Cloud schedules created: {cloud_schedules_created}\n'
        ))