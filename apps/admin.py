
from apps.console.account.models import CoreAccount, CoreAccountMembership
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.cloud.digitalocean.models import (
    CoreDigitalOceanAccount, CoreDigitalOceanServer, CoreDigitalOceanDatabase,
    CoreDigitalOceanVolume
)
from apps.console.cloud.hetzner.models import (
    CoreHetznerAccount, CoreHetznerServer, CoreHetznerVolume
)
from apps.console.cloud.vultr.models import (
    CoreVultrAccount, CoreVultrServer, CoreVultrDatabase, CoreVultrVolume
)
from apps.console.cloud.aws.models import (
    CoreAWSAccount, CoreAWSInstance, CoreAWSVolume, CoreAWSRDSDatabase, CoreAWSLambda, CoreAWSDynamoDB, CoreAWSS3Bucket, CoreAWSACMCertificate, CoreAWSSnapshot, CoreAWSElasticIP, CoreAWSLoadBalancer, CoreAWSSecurityGroup, CoreAWSECSService, CoreAWSECSTask
)
from apps.console.member.models import CoreMember
from apps.console.plan.models import CorePlan
from django.contrib import admin


@admin.register(CoreMember)
class CoreMemberAdmin(admin.ModelAdmin):
    list_display = ('user', 'active_account', 'email_verified')
    search_fields = ('user__username', 'user__email')
    list_filter = ('email_verified',)


@admin.register(CoreAccount)
class CoreAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'status', 'plan', 'created_at')
    list_filter = ('status', 'plan', 'is_default')
    search_fields = ('name', 'owner__username')
    date_hierarchy = 'created_at'


@admin.register(CoreAccountMembership)
class CoreAccountMembershipAdmin(admin.ModelAdmin):
    list_display = ('member', 'account', 'role', 'joined_at')
    list_filter = ('role',)
    search_fields = ('member__user__username', 'account__name')


@admin.register(CorePlan)
class CorePlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'type', 'max_clouds', 'max_assets', 'max_team_members')
    list_filter = ('type', 'enable_api_access', 'enable_team_members', 'enable_custom_monitoring')
    search_fields = ('name',)


@admin.register(CoreCloud)
class CoreCloudAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'owner_email', 'provider', 'status', 'last_synced')
    list_filter = ('status', 'provider')
    search_fields = ('account__name', 'account__owner__email')
    date_hierarchy = 'created'

    def display_name(self, obj):
        return obj.name
    display_name.short_description = 'Cloud Account'

    def owner_email(self, obj):
        return obj.account.owner.email
    owner_email.short_description = 'Owner Email'


@admin.register(CoreCloudServiceProvider)
class CoreCloudServiceProviderAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'status', 'position')
    list_filter = ('status',)
    search_fields = ('name', 'code')


# DigitalOcean Admin
@admin.register(CoreDigitalOceanAccount)
class CoreDigitalOceanAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'cloud', 'status', 'last_synced')
    list_filter = ('status',)
    search_fields = ('name',)


@admin.register(CoreDigitalOceanServer)
class CoreDigitalOceanServerAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreDigitalOceanVolume)
class CoreDigitalOceanVolumeAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreDigitalOceanDatabase)
class CoreDigitalOceanDatabaseAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


# Hetzner Admin
@admin.register(CoreHetznerAccount)
class CoreHetznerAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'cloud', 'status', 'last_synced')
    list_filter = ('status',)
    search_fields = ('name',)


@admin.register(CoreHetznerServer)
class CoreHetznerServerAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreHetznerVolume)
class CoreHetznerVolumeAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


# Vultr Admin
@admin.register(CoreVultrAccount)
class CoreVultrAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'cloud', 'status', 'last_synced')
    list_filter = ('status',)
    search_fields = ('name',)


@admin.register(CoreVultrServer)
class CoreVultrServerAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreVultrVolume)
class CoreVultrVolumeAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreVultrDatabase)
class CoreVultrDatabaseAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


# AWS Admin
@admin.register(CoreAWSAccount)
class CoreAWSAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'cloud', 'region', 'status', 'last_synced')
    list_filter = ('status', 'region')
    search_fields = ('name', 'region')


@admin.register(CoreAWSInstance)
class CoreAWSInstanceAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSVolume)
class CoreAWSVolumeAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSRDSDatabase)
class CoreAWSRDSDatabaseAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSLambda)
class CoreAWSLambdaAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSDynamoDB)
class CoreAWSDynamoDBAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSS3Bucket)
class CoreAWSS3BucketAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSACMCertificate)
class CoreAWSACMCertificateAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSSnapshot)
class CoreAWSSnapshotAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSElasticIP)
class CoreAWSElasticIPAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSLoadBalancer)
class CoreAWSLoadBalancerAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSSecurityGroup)
class CoreAWSSecurityGroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSECSService)
class CoreAWSECSServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')


@admin.register(CoreAWSECSTask)
class CoreAWSECSTaskAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'monitoring', 'type')
    list_filter = ('monitoring', 'type')
    search_fields = ('name', 'unique_id')
