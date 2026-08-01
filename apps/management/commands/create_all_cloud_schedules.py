from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask

from apps.console.cloud.models import CoreCloud
from apps.monitoring.schedules import cloud_schedule_update


class Command(BaseCommand):
    help = 'Create or repair monitoring schedules for active and recovering clouds'

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
                'This command will create or repair monitoring schedules for active and recovering clouds.\n'
                'To confirm, run the command with --confirm flag.'
            ))
            return

        # Get active clouds or a specific cloud
        if cloud_id:
            clouds = CoreCloud.objects.filter(id=cloud_id)
            self.stdout.write(self.style.NOTICE(f'Processing only cloud ID: {cloud_id}'))
        else:
            clouds = CoreCloud.objects.filter(
                status__in=(CoreCloud.Status.ACTIVE, CoreCloud.Status.INVALID_AUTH)
            )
            self.stdout.write(
                self.style.NOTICE(
                    f'Processing {clouds.count()} active or recovering clouds'
                )
            )

        # Track creation statistics
        cloud_schedules_created = 0
        clouds_processed = 0

        # Process each cloud
        for cloud in clouds:
            try:
                self.stdout.write(f'Processing cloud: {cloud.name} (ID: {cloud.id})')

                # Keep active and invalid-auth clouds scheduled. The latter
                # must continue validating credentials so it can recover.
                if cloud.status in (CoreCloud.Status.ACTIVE, CoreCloud.Status.INVALID_AUTH):
                    # Upsert the cloud schedule so this command also repairs
                    # stale task names, kwargs, intervals, or enabled state.
                    had_schedule = PeriodicTask.objects.filter(name=f'cloud-{cloud.uuid}').exists()
                    self.stdout.write(f'  {"Repairing" if had_schedule else "Creating"} cloud schedule for cloud {cloud.id}...')
                    cloud_schedule_update(cloud)
                    if not had_schedule:
                        cloud_schedules_created += 1
                    self.stdout.write(self.style.SUCCESS(f'  Cloud schedule ready for {cloud.name}'))
                    # Create schedules for all active assets
                    self.stdout.write(f'  Reconciling asset schedules for cloud {cloud.id}...')
                    cloud.create_all_asset_schedules()
                    self.stdout.write(self.style.SUCCESS(f'  Created asset schedules for cloud {cloud.name}'))
                else:
                    self.stdout.write(self.style.WARNING(f'  Skipping cloud {cloud.id} - not eligible for monitoring'))

                clouds_processed += 1

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error processing cloud {cloud.id}: {str(e)}'))

        # Print summary
        self.stdout.write(self.style.SUCCESS(
            f'\nSchedule creation completed:\n'
            f'- Clouds processed: {clouds_processed}\n'
            f'- Cloud schedules created: {cloud_schedules_created}\n'
        ))
