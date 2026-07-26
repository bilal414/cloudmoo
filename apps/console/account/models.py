from django.contrib.auth.models import User
from django.db import models

from apps.console.plan.models import CorePlan, get_default_plan_id


class CoreAccount(models.Model):
    class Status(models.IntegerChoices):
        INACTIVE = 0, 'Inactive'
        ACTIVE = 1, 'Active'
        SUSPENDED = 2, 'Suspended'

    name = models.CharField(max_length=255)
    status = models.IntegerField(choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_accounts')
    members = models.ManyToManyField('CoreMember', related_name='accounts', through='CoreAccountMembership')
    is_default = models.BooleanField(default=False)

    # Plan controlling limits (clouds, assets, team members) for this account.
    # Self-hosted instances use the single "Self-Hosted" plan by default;
    # admins can adjust limits through the Django admin.
    plan = models.ForeignKey(
        CorePlan,
        on_delete=models.PROTECT,
        related_name='accounts',
        default=get_default_plan_id
    )

    class Meta:
        db_table = 'core_account'
        verbose_name = "Account"
        verbose_name_plural = "Accounts"

    def __str__(self):
        return self.name

    def delete(self, *args, **kwargs):
        if self.is_default:
            raise ValueError("Default account cannot be deleted.")
        super().delete(*args, **kwargs)

    def get_total_assets(self):
        from apps.console.utils.models import UtilAsset

        """Calculate total assets across all clouds"""
        total = 0
        for cloud in self.clouds.all():
            provider_account = cloud.provider_account
            # Count servers
            if hasattr(provider_account, 'servers'):
                total += provider_account.servers.exclude(
                    monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                ).count()
            # Count volumes
            if hasattr(provider_account, 'volumes'):
                total += provider_account.volumes.exclude(
                    monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                ).count()
            # Count databases
            if hasattr(provider_account, 'databases'):
                total += provider_account.databases.exclude(
                    monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                ).count()
        return total

    # Helper methods to check plan limits
    def has_reached_cloud_limit(self):
        return self.clouds.count() >= self.plan.max_clouds

    def has_reached_asset_limit(self):
        return self.get_total_assets() >= self.plan.max_assets

    def has_reached_team_member_limit(self):
        if not self.plan.enable_team_members:
            return self.members.count() > 1
        return self.members.count() >= self.plan.max_team_members

    @property
    def monitoring_interval(self):
        return self.plan.monitoring_interval

    @property
    def log_retention_days(self):
        return self.plan.log_retention_days


class CoreAccountMembership(models.Model):
    class Role(models.IntegerChoices):
        MEMBER = 0, 'Member'
        ADMIN = 1, 'Admin'
        OWNER = 2, 'Owner'

    account = models.ForeignKey(CoreAccount, on_delete=models.CASCADE, related_name='memberships')
    member = models.ForeignKey('CoreMember', on_delete=models.CASCADE, related_name='memberships')
    role = models.IntegerField(choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_account_membership'
        unique_together = ('account', 'member')

    def __str__(self):
        return f"{self.member.user.username} - {self.account.name} ({self.get_role_display()})"
