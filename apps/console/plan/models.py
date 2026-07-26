from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class CorePlan(models.Model):
    class Type(models.TextChoices):
        SELFHOSTED = 'selfhosted', 'Self-Hosted'
        # Legacy SaaS plan types, kept for backward compatibility with
        # databases migrated from the hosted version of CloudMoo.
        HACKER = 'hacker', 'Hacker'
        STARTUP = 'startup', 'Startup'
        ENTERPRISE = 'enterprise', 'Enterprise'

    name = models.CharField(max_length=100)
    type = models.CharField(max_length=20, choices=Type.choices, unique=True)
    description = models.TextField(blank=True)

    # Asset and cloud limits
    max_clouds = models.PositiveIntegerField(
        help_text="Maximum number of clouds allowed"
    )
    max_assets = models.PositiveIntegerField(
        help_text="Maximum number of total assets allowed (servers + volumes + databases)"
    )

    # Monitoring settings
    monitoring_interval = models.PositiveIntegerField(
        help_text="Monitoring interval in minutes",
        validators=[MinValueValidator(1), MaxValueValidator(60)]
    )

    # Log retention
    log_retention_days = models.PositiveIntegerField(
        help_text="Number of days to retain logs",
        validators=[MinValueValidator(1)]
    )

    # Additional features
    enable_api_access = models.BooleanField(default=False)
    enable_team_members = models.BooleanField(default=False)
    max_team_members = models.PositiveIntegerField(default=1)
    enable_custom_monitoring = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'core_plan'
        verbose_name = "Plan"
        verbose_name_plural = "Plans"

    def __str__(self):
        return self.name

    @classmethod
    def get_default(cls):
        """
        Return the default plan for new accounts on a self-hosted instance.
        Created on first use with (effectively) unlimited limits; instance
        admins can adjust it through the Django admin.
        """
        plan, _created = cls.objects.get_or_create(
            type=cls.Type.SELFHOSTED,
            defaults={
                'name': 'Self-Hosted',
                'description': 'Default plan for self-hosted CloudMoo instances.',
                'max_clouds': 1000,
                'max_assets': 10000,
                'monitoring_interval': 1,
                'log_retention_days': 30,
                'enable_api_access': True,
                'enable_team_members': True,
                'max_team_members': 1000,
                'enable_custom_monitoring': True,
            },
        )
        return plan


def get_default_plan_id():
    """Migration-serializable default for CoreAccount.plan."""
    return CorePlan.get_default().pk
