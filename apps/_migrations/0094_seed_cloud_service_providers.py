# Seed the built-in cloud service providers so fresh installs have a working
# "Connect" page without running setup_test_accounts.

from django.db import migrations

PROVIDERS = [
    ("digitalocean", "DigitalOcean"),
    ("aws", "Amazon Web Services"),
    ("hetzner", "Hetzner"),
    ("vultr", "Vultr"),
    ("upcloud", "UpCloud"),
    ("linode", "Linode"),
]


def seed_providers(apps, schema_editor):
    CoreCloudServiceProvider = apps.get_model("apps", "CoreCloudServiceProvider")
    for position, (code, name) in enumerate(PROVIDERS, start=1):
        CoreCloudServiceProvider.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "position": position,
                "image": f"console/images/clouds/{code}.svg",
                "status": "active",
            },
        )


def unseed_providers(apps, schema_editor):
    CoreCloudServiceProvider = apps.get_model("apps", "CoreCloudServiceProvider")
    CoreCloudServiceProvider.objects.filter(
        code__in=[code for code, _name in PROVIDERS]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("apps", "0093_alter_coreawsacmcertificate_type_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_providers, unseed_providers),
    ]
