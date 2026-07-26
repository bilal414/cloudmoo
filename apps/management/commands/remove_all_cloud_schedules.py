from django.core.management.base import BaseCommand
from apps.console.cloud.models import CoreCloud
import time


class Command(BaseCommand):
    help = 'Remove all cloud schedules and asset schedules from AWS EventBridge'

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Confirmation flag to proceed with deletion',
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
                'This command will remove ALL cloud and asset schedules from AWS EventBridge.\n'
                'To confirm, run the command with --confirm flag.'
            ))
            return

        # Get all clouds or a specific cloud
        if cloud_id:
            clouds = CoreCloud.objects.filter(id=cloud_id)
            self.stdout.write(self.style.NOTICE(f'Processing only cloud ID: {cloud_id}'))
        else:
            clouds = CoreCloud.objects.all()
            self.stdout.write(self.style.NOTICE(f'Processing all {clouds.count()} clouds'))

        # Track deletion statistics
        cloud_schedules_deleted = 0
        clouds_processed = 0

        # Process each cloud
        for cloud in clouds:
            try:
                self.stdout.write(f'Processing cloud: {cloud.name} (ID: {cloud.id})')

                # Delete all asset schedules for this cloud
                self.stdout.write(f'  Deleting asset schedules for cloud {cloud.id}...')
                cloud.delete_all_asset_schedules()

                # Delete cloud's own schedule
                self.stdout.write(f'  Deleting cloud schedule for cloud {cloud.id}...')
                cloud.aws_schedule_delete()

                cloud_schedules_deleted += 1
                clouds_processed += 1
                self.stdout.write(self.style.SUCCESS(f'  Successfully removed schedules for cloud: {cloud.name}'))

                # Small delay to prevent API throttling
                time.sleep(0.5)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error processing cloud {cloud.id}: {str(e)}'))

        # Print summary
        self.stdout.write(self.style.SUCCESS(
            f'\nDeletion completed:\n'
            f'- Clouds processed: {clouds_processed}\n'
            f'- Cloud schedules deleted: {cloud_schedules_deleted}\n'
        ))