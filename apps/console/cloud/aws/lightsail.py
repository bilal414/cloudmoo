"""Amazon Lightsail inventory models and read-only synchronization.

Lightsail is part of AWS, but its resources do not use the EC2/RDS/ELB
identifiers or state shapes used by the existing AWS adapter.  Keeping these
assets in their own models lets CloudMoo monitor Lightsail without conflating
an instance with an EC2 instance, or a Lightsail bucket with an S3 bucket.

The synchronization path intentionally uses only Lightsail ``Get*`` APIs.
It never provisions, updates, attaches, detaches, stops, starts, or deletes a
provider resource.
"""

import hashlib
import logging
from urllib.parse import quote

from django.db import models

from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.utils.helper import _serialize_datetime
from apps.console.utils.models import UtilAsset
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger(__name__)

LIGHTSAIL_GLOBAL_REGION = "us-east-1"


def _lightsail_owner_identifier_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "unique_id"),
        name=f"aws_ls_{name}_uid_uniq",
    )


def _error_code(error):
    response = getattr(error, "response", {}) or {}
    details = response.get("Error", {}) if isinstance(response, dict) else {}
    return details.get("Code") or type(error).__name__


def _resource_key(asset_type, region, name, suffix=None):
    """Build a stable, bounded local identifier for a Lightsail resource."""
    raw = ":".join(str(value) for value in (asset_type, region, name, suffix) if value)
    if len(raw) <= 100:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
    return f"{asset_type}:{region}:{digest}"


def _item_name(item, fallback="unknown"):
    if not isinstance(item, dict):
        return fallback
    for key in (
        "name",
        "certificateName",
        "bucketName",
        "distributionName",
        "loadBalancerName",
        "instanceName",
        "diskName",
        "instanceSnapshotName",
        "diskSnapshotName",
        "staticIpName",
        "relationalDatabaseName",
        "relationalDatabaseSnapshotName",
        "alarmName",
        "domainName",
        "containerServiceName",
        "serviceName",
        "resourceName",
        "operationId",
        "id",
        "version",
        "image",
        "date",
    ):
        value = item.get(key)
        if value is not None and str(value):
            return str(value)
    return fallback


def _display_name(value, fallback="Lightsail resource"):
    value = str(value or fallback)
    return value[:100]


def _with_context(item, region, resource_type, resource_name, **extra):
    metadata = _serialize_datetime(item if isinstance(item, dict) else {})
    metadata["_cloudmoo_region"] = region
    metadata["_cloudmoo_resource_type"] = resource_type
    metadata["_cloudmoo_name"] = resource_name
    metadata.update(extra)
    return metadata


def _upsert_asset(model, account, unique_id, name, asset_type, metadata, monitoring=None):
    defaults = {
        "name": _display_name(name),
        "type": asset_type,
        "metadata": metadata,
    }
    if monitoring is not None:
        defaults["monitoring"] = monitoring

    asset, created = model.objects.get_or_create(
        owner=account,
        unique_id=unique_id,
        defaults=defaults,
    )
    asset.name = _display_name(name)
    asset.type = asset_type
    asset.metadata = metadata
    if not created and asset.monitoring == UtilAsset.Monitoring.NO_LONGER_EXISTS:
        # A transiently incomplete provider response must not permanently hide
        # a resource that appears again.  Preserve an explicit DISABLED choice.
        asset.monitoring = monitoring or UtilAsset.Monitoring.ACTIVE
    asset.save()
    return asset


class CoreAWSLightsailAsset(UtilAsset):
    """Abstract common behavior for all Lightsail asset records."""

    class Meta:
        abstract = True

    @property
    def lightsail_region(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        return metadata.get("_cloudmoo_region") or self.owner.region

    @property
    def lightsail_resource_name(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        return metadata.get("_cloudmoo_name") or metadata.get("name") or self.name

    @property
    def provider_url(self):
        region = quote(str(self.lightsail_region), safe="-")
        return f"https://lightsail.aws.amazon.com/ls/webapp/home/{region}/home"

    @property
    def monitoring_credentials(self):
        """Provide the regional read-only context required by the checker."""
        return {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.lightsail_region,
            "resource_region": self.lightsail_region,
            "resource_name": self.lightsail_resource_name,
            "asset_type": self.type,
            "metadata": self.metadata if isinstance(self.metadata, dict) else {},
        }

    def check_status(self):
        from apps.monitoring.checks.aws_lightsail import check_lightsail_resource_status

        return check_lightsail_resource_status(self.type, self.unique_id, self.monitoring_credentials)


class CoreAWSLightsailInstance(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_instances")

    class Meta:
        db_table = "core_aws_lightsail_instance"
        constraints = [_lightsail_owner_identifier_constraint("instance")]


class CoreAWSLightsailDisk(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_disks")

    class Meta:
        db_table = "core_aws_lightsail_disk"
        constraints = [_lightsail_owner_identifier_constraint("disk")]


class CoreAWSLightsailInstanceSnapshot(CoreAWSLightsailAsset):
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="lightsail_instance_snapshots",
    )

    class Meta:
        db_table = "core_aws_lightsail_instance_snapshot"
        constraints = [_lightsail_owner_identifier_constraint("instance_snapshot")]


class CoreAWSLightsailDiskSnapshot(CoreAWSLightsailAsset):
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="lightsail_disk_snapshots",
    )

    class Meta:
        db_table = "core_aws_lightsail_disk_snapshot"
        constraints = [_lightsail_owner_identifier_constraint("disk_snapshot")]


class CoreAWSLightsailStaticIP(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_static_ips")

    class Meta:
        db_table = "core_aws_lightsail_static_ip"
        constraints = [_lightsail_owner_identifier_constraint("static_ip")]


class CoreAWSLightsailDatabase(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_databases")

    class Meta:
        db_table = "core_aws_lightsail_database"
        constraints = [_lightsail_owner_identifier_constraint("database")]


class CoreAWSLightsailDatabaseSnapshot(CoreAWSLightsailAsset):
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="lightsail_database_snapshots",
    )

    class Meta:
        db_table = "core_aws_lightsail_database_snapshot"
        constraints = [_lightsail_owner_identifier_constraint("database_snapshot")]


class CoreAWSLightsailLoadBalancer(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_load_balancers")

    class Meta:
        db_table = "core_aws_lightsail_load_balancer"
        constraints = [_lightsail_owner_identifier_constraint("load_balancer")]


class CoreAWSLightsailCertificate(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_certificates")

    class Meta:
        db_table = "core_aws_lightsail_certificate"
        constraints = [_lightsail_owner_identifier_constraint("certificate")]


class CoreAWSLightsailBucket(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_buckets")

    class Meta:
        db_table = "core_aws_lightsail_bucket"
        constraints = [_lightsail_owner_identifier_constraint("bucket")]


class CoreAWSLightsailDistribution(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_distributions")

    class Meta:
        db_table = "core_aws_lightsail_distribution"
        constraints = [_lightsail_owner_identifier_constraint("distribution")]


class CoreAWSLightsailDomain(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_domains")

    class Meta:
        db_table = "core_aws_lightsail_domain"
        constraints = [_lightsail_owner_identifier_constraint("domain")]


class CoreAWSLightsailDNSRecord(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_dns_records")

    class Meta:
        db_table = "core_aws_lightsail_dns_record"
        constraints = [_lightsail_owner_identifier_constraint("dns_record")]


class CoreAWSLightsailContainerService(CoreAWSLightsailAsset):
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="lightsail_container_services",
    )

    class Meta:
        db_table = "core_aws_lightsail_container_service"
        constraints = [_lightsail_owner_identifier_constraint("container_service")]


class CoreAWSLightsailContainerDeployment(CoreAWSLightsailAsset):
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="lightsail_container_deployments",
    )

    class Meta:
        db_table = "core_aws_lightsail_container_deployment"
        constraints = [_lightsail_owner_identifier_constraint("container_deployment")]


class CoreAWSLightsailContainerImage(CoreAWSLightsailAsset):
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="lightsail_container_images",
    )

    class Meta:
        db_table = "core_aws_lightsail_container_image"
        constraints = [_lightsail_owner_identifier_constraint("container_image")]


class CoreAWSLightsailAlarm(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_alarms")

    class Meta:
        db_table = "core_aws_lightsail_alarm"
        constraints = [_lightsail_owner_identifier_constraint("alarm")]


class CoreAWSLightsailOperation(CoreAWSLightsailAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name="lightsail_operations")

    class Meta:
        db_table = "core_aws_lightsail_operation"
        constraints = [_lightsail_owner_identifier_constraint("operation")]


class CoreAWSLightsailAutoSnapshot(CoreAWSLightsailAsset):
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="lightsail_auto_snapshots",
    )

    class Meta:
        db_table = "core_aws_lightsail_auto_snapshot"
        constraints = [_lightsail_owner_identifier_constraint("auto_snapshot")]


LIGHTSAIL_COLLECTION_SPECS = (
    {
        "model": CoreAWSLightsailInstance,
        "asset_type": UtilAsset.Type.LIGHTSAIL_INSTANCE,
        "operation": "get_instances",
        "response_key": "instances",
        "resource_type": "Instance",
        "scope": "regional",
        "auto_snapshots": True,
    },
    {
        "model": CoreAWSLightsailDisk,
        "asset_type": UtilAsset.Type.LIGHTSAIL_DISK,
        "operation": "get_disks",
        "response_key": "disks",
        "resource_type": "Disk",
        "scope": "regional",
        "auto_snapshots": True,
    },
    {
        "model": CoreAWSLightsailInstanceSnapshot,
        "asset_type": UtilAsset.Type.LIGHTSAIL_INSTANCE_SNAPSHOT,
        "operation": "get_instance_snapshots",
        "response_key": "instanceSnapshots",
        "resource_type": "InstanceSnapshot",
        "scope": "regional",
    },
    {
        "model": CoreAWSLightsailDiskSnapshot,
        "asset_type": UtilAsset.Type.LIGHTSAIL_DISK_SNAPSHOT,
        "operation": "get_disk_snapshots",
        "response_key": "diskSnapshots",
        "resource_type": "DiskSnapshot",
        "scope": "regional",
    },
    {
        "model": CoreAWSLightsailStaticIP,
        "asset_type": UtilAsset.Type.LIGHTSAIL_STATIC_IP,
        "operation": "get_static_ips",
        "response_key": "staticIps",
        "resource_type": "StaticIp",
        "scope": "regional",
    },
    {
        "model": CoreAWSLightsailDatabase,
        "asset_type": UtilAsset.Type.LIGHTSAIL_DATABASE,
        "operation": "get_relational_databases",
        "response_key": "relationalDatabases",
        "resource_type": "RelationalDatabase",
        "scope": "regional",
    },
    {
        "model": CoreAWSLightsailDatabaseSnapshot,
        "asset_type": UtilAsset.Type.LIGHTSAIL_DATABASE_SNAPSHOT,
        "operation": "get_relational_database_snapshots",
        "response_key": "relationalDatabaseSnapshots",
        "resource_type": "RelationalDatabaseSnapshot",
        "scope": "regional",
    },
    {
        "model": CoreAWSLightsailLoadBalancer,
        "asset_type": UtilAsset.Type.LIGHTSAIL_LOAD_BALANCER,
        "operation": "get_load_balancers",
        "response_key": "loadBalancers",
        "resource_type": "LoadBalancer",
        "scope": "regional",
    },
    {
        "model": CoreAWSLightsailCertificate,
        "asset_type": UtilAsset.Type.LIGHTSAIL_CERTIFICATE,
        "operation": "get_certificates",
        "response_key": "certificates",
        "resource_type": "Certificate",
        "scope": "regional",
        "list_kwargs": {"includeCertificateDetails": True},
    },
    {
        "model": CoreAWSLightsailBucket,
        "asset_type": UtilAsset.Type.LIGHTSAIL_BUCKET,
        "operation": "get_buckets",
        "response_key": "buckets",
        "resource_type": "Bucket",
        "scope": "regional",
        # IncludeCors is only valid when bucketName is supplied.  The sync
        # enriches each returned bucket with a read-only detail request below.
        "list_kwargs": {"includeConnectedResources": True},
    },
    {
        "model": CoreAWSLightsailDistribution,
        "asset_type": UtilAsset.Type.LIGHTSAIL_DISTRIBUTION,
        "operation": "get_distributions",
        "response_key": "distributions",
        "resource_type": "Distribution",
        "scope": "global",
    },
    {
        "model": CoreAWSLightsailDomain,
        "asset_type": UtilAsset.Type.LIGHTSAIL_DOMAIN,
        "operation": "get_domains",
        "response_key": "domains",
        "resource_type": "Domain",
        "scope": "global",
    },
    {
        "model": CoreAWSLightsailContainerService,
        "asset_type": UtilAsset.Type.LIGHTSAIL_CONTAINER_SERVICE,
        "operation": "get_container_services",
        "response_key": "containerServices",
        "resource_type": "ContainerService",
        "scope": "regional",
    },
    {
        "model": CoreAWSLightsailAlarm,
        "asset_type": UtilAsset.Type.LIGHTSAIL_ALARM,
        "operation": "get_alarms",
        "response_key": "alarms",
        "resource_type": "Alarm",
        "scope": "regional",
    },
    {
        "model": CoreAWSLightsailOperation,
        "asset_type": UtilAsset.Type.LIGHTSAIL_OPERATION,
        "operation": "get_operations",
        "response_key": "operations",
        "resource_type": "Operation",
        "scope": "regional",
    },
)


def _lightsail_collection(client, operation, response_key, **kwargs):
    """Read a paginated Lightsail collection and fail closed on bad pages."""
    values = []
    page_token = None
    while True:
        request = dict(kwargs)
        if page_token:
            request["pageToken"] = page_token
        response = getattr(client, operation)(**request)
        if not isinstance(response, dict) or response.get(response_key) is None:
            raise CloudInventoryTransientError(
                f"AWS Lightsail returned an incomplete {operation} response"
            )
        page_values = response[response_key]
        if not isinstance(page_values, list):
            raise CloudInventoryTransientError(
                f"AWS Lightsail returned an invalid {operation} collection"
            )
        values.extend(page_values)
        page_token = response.get("nextPageToken")
        if not page_token:
            return values


def _lightsail_regions(account):
    client = account._get_aws_client("lightsail")
    response = client.get_regions()
    regions = response.get("regions") if isinstance(response, dict) else None
    if not isinstance(regions, list):
        raise CloudInventoryTransientError("AWS Lightsail returned an invalid regions collection")

    available = set()
    for region in regions:
        if not isinstance(region, dict):
            continue
        name = region.get("name") or region.get("regionName")
        state = region.get("state")
        normalized_state = str(state).lower() if state is not None else None
        if name and normalized_state in (None, "available", "active"):
            available.add(name)

    # Keep an explicitly configured region in scope if an older account/API
    # response omits it, but never invent a region outside the account config.
    available.add(account.region)
    return sorted(available)


def _regions_for_spec(regions, spec):
    return [LIGHTSAIL_GLOBAL_REGION] if spec.get("scope") == "global" else regions


def _instance_port_states(client, instance_name):
    response = client.get_instance_port_states(instanceName=instance_name)
    return response.get("portStates", []) if isinstance(response, dict) else []


def _collection_metadata(item, region, spec):
    resource_name = _item_name(item)
    display_name = resource_name
    if spec["asset_type"] == UtilAsset.Type.LIGHTSAIL_OPERATION:
        operation_id = str(item.get("id") or resource_name)
        resource_name = operation_id
        display_name = f"{item.get('resourceName') or 'operation'} / {operation_id}"
    metadata = _with_context(item, region, spec["resource_type"], resource_name)
    if display_name != resource_name:
        metadata["_cloudmoo_display_name"] = display_name[:100]
    if spec["asset_type"] == UtilAsset.Type.LIGHTSAIL_INSTANCE:
        # Port states are part of the instance firewall surface.  This is a
        # read-only detail call; if it is unavailable, preserve the instance
        # and record the provider error without treating it as empty.
        metadata["portStates"] = None
    return resource_name, metadata


def _sync_collection(account, spec, regions):
    model = spec["model"]
    items = []
    errors = []
    for region in _regions_for_spec(regions, spec):
        try:
            client = account._get_aws_client("lightsail", region=region)
            region_items = _lightsail_collection(
                client,
                spec["operation"],
                spec["response_key"],
                **spec.get("list_kwargs", {}),
            )
            items.extend((region, item) for item in region_items)
        except (ClientError, BotoCoreError, CloudInventoryTransientError) as error:
            errors.append(f"{region}:{_error_code(error)}")
        except Exception as error:
            errors.append(f"{region}:{type(error).__name__}")

    if errors:
        raise CloudInventoryTransientError(
            f"AWS Lightsail {spec['operation']} failed in {', '.join(errors)}"
        )

    current_ids = []
    contexts = []
    for region, item in items:
        resource_name, metadata = _collection_metadata(item, region, spec)
        if spec["asset_type"] == UtilAsset.Type.LIGHTSAIL_BUCKET:
            # Lightsail rejects includeCors on an unscoped collection call.
            # Fetch the CORS-enabled representation per bucket, but keep the
            # inventory usable if that optional enrichment is unavailable.
            try:
                bucket_items = _lightsail_collection(
                    account._get_aws_client("lightsail", region=region),
                    "get_buckets",
                    "buckets",
                    bucketName=resource_name,
                    includeConnectedResources=True,
                    includeCors=True,
                )
                if bucket_items:
                    enriched_item = dict(item)
                    enriched_item.update(bucket_items[0])
                    resource_name, metadata = _collection_metadata(
                        enriched_item,
                        region,
                        spec,
                    )
            except Exception as error:
                metadata["corsError"] = _error_code(error)
        if spec["asset_type"] == UtilAsset.Type.LIGHTSAIL_INSTANCE:
            try:
                metadata["portStates"] = _instance_port_states(
                    account._get_aws_client("lightsail", region=region),
                    resource_name,
                )
            except Exception as error:
                metadata["portStatesError"] = _error_code(error)

        unique_id = _resource_key(spec["asset_type"], region, resource_name)
        _upsert_asset(
            model,
            account,
            unique_id,
            metadata.get("_cloudmoo_display_name", resource_name),
            spec["asset_type"],
            metadata,
            monitoring=spec.get("monitoring"),
        )
        current_ids.append(unique_id)
        contexts.append({
            "region": region,
            "name": resource_name,
            "asset_type": spec["asset_type"],
            "resource_type": spec["resource_type"],
            "metadata": metadata,
        })

    model.objects.filter(owner=account).exclude(unique_id__in=current_ids).update(
        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
    )
    return contexts


def _sync_domains_and_records(account, domain_contexts):
    record_items = []
    errors = []
    for context in domain_contexts:
        try:
            client = account._get_aws_client("lightsail", region=context["region"])
            response = client.get_domain(domainName=context["name"])
            domain = response.get("domain") if isinstance(response, dict) else None
            if not isinstance(domain, dict):
                raise CloudInventoryTransientError("AWS Lightsail returned an invalid domain response")
            entries = domain.get("domainEntries")
            if not isinstance(entries, list):
                raise CloudInventoryTransientError("AWS Lightsail returned an invalid DNS record collection")
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                record_name = _item_name(entry, "@")
                record_id = entry.get("id") or ":".join(
                    str(entry.get(key, "")) for key in ("name", "type", "target")
                )
                metadata = _with_context(
                    entry,
                    context["region"],
                    "DomainEntry",
                    record_name,
                    _cloudmoo_domain_name=context["name"],
                    _cloudmoo_domain_arn=domain.get("arn"),
                )
                record_items.append((context["region"], context["name"], record_id, record_name, metadata))
        except (ClientError, BotoCoreError, CloudInventoryTransientError) as error:
            errors.append(f"{context['region']}:{context['name']}:{_error_code(error)}")
        except Exception as error:
            errors.append(f"{context['region']}:{context['name']}:{type(error).__name__}")

    if errors:
        logger.warning("Lightsail DNS record sync incomplete: %s", ", ".join(errors))

    current_ids = []
    for region, domain_name, record_id, record_name, metadata in record_items:
        unique_id = _resource_key(
            UtilAsset.Type.LIGHTSAIL_DNS_RECORD,
            region,
            domain_name,
            record_id,
        )
        _upsert_asset(
            CoreAWSLightsailDNSRecord,
            account,
            unique_id,
            f"{domain_name} / {record_name}",
            UtilAsset.Type.LIGHTSAIL_DNS_RECORD,
            metadata,
        )
        current_ids.append(unique_id)

    if not errors:
        CoreAWSLightsailDNSRecord.objects.filter(owner=account).exclude(unique_id__in=current_ids).update(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        )


def _sync_container_children(account, service_contexts):
    deployments = []
    images = []
    errors = []
    for context in service_contexts:
        try:
            client = account._get_aws_client("lightsail", region=context["region"])
            deployment_response = client.get_container_service_deployments(serviceName=context["name"])
            image_response = client.get_container_images(serviceName=context["name"])
            deployment_values = deployment_response.get("deployments") if isinstance(deployment_response, dict) else None
            image_values = image_response.get("containerImages") if isinstance(image_response, dict) else None
            if not isinstance(deployment_values, list) or not isinstance(image_values, list):
                raise CloudInventoryTransientError("AWS Lightsail returned an invalid container detail response")
            for item in deployment_values:
                if isinstance(item, dict):
                    version = _item_name(item)
                    metadata = _with_context(
                        item,
                        context["region"],
                        "ContainerServiceDeployment",
                        version,
                        _cloudmoo_service_name=context["name"],
                    )
                    deployments.append((context["region"], context["name"], version, metadata))
            for item in image_values:
                if isinstance(item, dict):
                    image_name = _item_name(item)
                    metadata = _with_context(
                        item,
                        context["region"],
                        "ContainerImage",
                        image_name,
                        _cloudmoo_service_name=context["name"],
                    )
                    images.append((context["region"], context["name"], image_name, metadata))
        except (ClientError, BotoCoreError, CloudInventoryTransientError) as error:
            errors.append(f"{context['region']}:{context['name']}:{_error_code(error)}")
        except Exception as error:
            errors.append(f"{context['region']}:{context['name']}:{type(error).__name__}")

    if errors:
        logger.warning("Lightsail container child sync incomplete: %s", ", ".join(errors))

    current_deployment_ids = []
    for region, service_name, version, metadata in deployments:
        unique_id = _resource_key(
            UtilAsset.Type.LIGHTSAIL_CONTAINER_DEPLOYMENT,
            region,
            service_name,
            version,
        )
        _upsert_asset(
            CoreAWSLightsailContainerDeployment,
            account,
            unique_id,
            f"{service_name} / deployment {version}",
            UtilAsset.Type.LIGHTSAIL_CONTAINER_DEPLOYMENT,
            metadata,
            monitoring=UtilAsset.Monitoring.DISABLED,
        )
        current_deployment_ids.append(unique_id)

    current_image_ids = []
    for region, service_name, image_name, metadata in images:
        unique_id = _resource_key(
            UtilAsset.Type.LIGHTSAIL_CONTAINER_IMAGE,
            region,
            service_name,
            image_name,
        )
        _upsert_asset(
            CoreAWSLightsailContainerImage,
            account,
            unique_id,
            f"{service_name} / {image_name}",
            UtilAsset.Type.LIGHTSAIL_CONTAINER_IMAGE,
            metadata,
            monitoring=UtilAsset.Monitoring.DISABLED,
        )
        current_image_ids.append(unique_id)

    if not errors:
        CoreAWSLightsailContainerDeployment.objects.filter(owner=account).exclude(
            unique_id__in=current_deployment_ids
        ).update(monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS)
        CoreAWSLightsailContainerImage.objects.filter(owner=account).exclude(
            unique_id__in=current_image_ids
        ).update(monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS)


def _sync_auto_snapshots(account, source_contexts):
    snapshots = []
    errors = []
    for context in source_contexts:
        try:
            client = account._get_aws_client("lightsail", region=context["region"])
            response = client.get_auto_snapshots(resourceName=context["name"])
            values = response.get("autoSnapshots") if isinstance(response, dict) else None
            if not isinstance(values, list):
                raise CloudInventoryTransientError("AWS Lightsail returned an invalid auto-snapshot collection")
            for item in values:
                if not isinstance(item, dict):
                    continue
                snapshot_name = _item_name(item, item.get("date") or "auto-snapshot")
                metadata = _with_context(
                    item,
                    context["region"],
                    "AutoSnapshot",
                    snapshot_name,
                    _cloudmoo_source_name=context["name"],
                    _cloudmoo_source_type=context["resource_type"],
                )
                snapshots.append((context["region"], context["name"], snapshot_name, metadata))
        except (ClientError, BotoCoreError, CloudInventoryTransientError) as error:
            errors.append(f"{context['region']}:{context['name']}:{_error_code(error)}")
        except Exception as error:
            errors.append(f"{context['region']}:{context['name']}:{type(error).__name__}")

    if errors:
        logger.warning("Lightsail auto-snapshot sync incomplete: %s", ", ".join(errors))

    current_ids = []
    for region, source_name, snapshot_name, metadata in snapshots:
        unique_id = _resource_key(
            UtilAsset.Type.LIGHTSAIL_AUTO_SNAPSHOT,
            region,
            source_name,
            snapshot_name,
        )
        _upsert_asset(
            CoreAWSLightsailAutoSnapshot,
            account,
            unique_id,
            f"{source_name} / {snapshot_name}",
            UtilAsset.Type.LIGHTSAIL_AUTO_SNAPSHOT,
            metadata,
        )
        current_ids.append(unique_id)

    if not errors:
        CoreAWSLightsailAutoSnapshot.objects.filter(owner=account).exclude(
            unique_id__in=current_ids
        ).update(monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS)


def sync_lightsail_assets(account):
    """Synchronize the complete read-only Lightsail inventory for an account."""
    regions = _lightsail_regions(account)
    contexts = {}
    for spec in LIGHTSAIL_COLLECTION_SPECS:
        contexts[spec["asset_type"]] = _sync_collection(account, spec, regions)

    _sync_domains_and_records(
        account,
        contexts.get(UtilAsset.Type.LIGHTSAIL_DOMAIN, []),
    )
    _sync_container_children(
        account,
        contexts.get(UtilAsset.Type.LIGHTSAIL_CONTAINER_SERVICE, []),
    )
    _sync_auto_snapshots(
        account,
        contexts.get(UtilAsset.Type.LIGHTSAIL_INSTANCE, [])
        + contexts.get(UtilAsset.Type.LIGHTSAIL_DISK, []),
    )

    counts = {
        spec["asset_type"]: spec["model"].objects.filter(owner=account).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).count()
        for spec in LIGHTSAIL_COLLECTION_SPECS
    }
    for asset_type, model in (
        (UtilAsset.Type.LIGHTSAIL_DNS_RECORD, CoreAWSLightsailDNSRecord),
        (UtilAsset.Type.LIGHTSAIL_CONTAINER_DEPLOYMENT, CoreAWSLightsailContainerDeployment),
        (UtilAsset.Type.LIGHTSAIL_CONTAINER_IMAGE, CoreAWSLightsailContainerImage),
        (UtilAsset.Type.LIGHTSAIL_AUTO_SNAPSHOT, CoreAWSLightsailAutoSnapshot),
    ):
        counts[asset_type] = model.objects.filter(owner=account).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).count()

    return {
        "regions": regions,
        "counts": counts,
    }


__all__ = [
    "CoreAWSLightsailAsset",
    "CoreAWSLightsailInstance",
    "CoreAWSLightsailDisk",
    "CoreAWSLightsailInstanceSnapshot",
    "CoreAWSLightsailDiskSnapshot",
    "CoreAWSLightsailStaticIP",
    "CoreAWSLightsailDatabase",
    "CoreAWSLightsailDatabaseSnapshot",
    "CoreAWSLightsailLoadBalancer",
    "CoreAWSLightsailCertificate",
    "CoreAWSLightsailBucket",
    "CoreAWSLightsailDistribution",
    "CoreAWSLightsailDomain",
    "CoreAWSLightsailDNSRecord",
    "CoreAWSLightsailContainerService",
    "CoreAWSLightsailContainerDeployment",
    "CoreAWSLightsailContainerImage",
    "CoreAWSLightsailAlarm",
    "CoreAWSLightsailOperation",
    "CoreAWSLightsailAutoSnapshot",
    "sync_lightsail_assets",
]
