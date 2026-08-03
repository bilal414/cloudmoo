# AWS resource integration map

This document separates AWS resource families that CloudMoo already supports
from resource families that are good candidates for future integration. The
scope is inventory and monitoring: CloudMoo should use read-only provider APIs
and write only to its own database.

The live discovery described below was performed on 2026-08-02. The AWS
credential file was mounted read-only inside a short-lived Docker container;
credential values and resource identifiers were not printed. No AWS resource
was created, changed, started, stopped, attached, detached, or deleted.

## Current CloudMoo coverage

| AWS family | Current CloudMoo coverage | Recommended interpretation |
| --- | --- | --- |
| EC2 | Instances, state/details, tags, EBS volumes, EBS snapshots, Elastic IPs, and security groups | First-class inventory and status monitoring |
| RDS | DB instances and status/details | First-class inventory and status monitoring |
| Lambda | Functions and state/details | First-class inventory and status monitoring |
| DynamoDB | Tables and table status/details | First-class inventory and status monitoring |
| S3 | Buckets and read-only bucket configuration checks | Inventory/configuration monitoring; buckets do not expose a single VM-like health state |
| ACM | Certificates and certificate status/details | First-class certificate-expiry/status monitoring |
| ELB/ELBv2 | Classic and modern load balancers | First-class inventory/status monitoring; target/listener health can be expanded |
| ECS | Services and tasks | First-class inventory/status monitoring; task definitions, deployments, and logs remain a gap |
| Lightsail | Instances, disks, instance/disk snapshots, static IPs, managed databases and snapshots, load balancers, certificates, buckets, distributions, domains/records, container services/deployments/images, alarms, operations, and auto-snapshots | Dedicated read-only adapter; see [Lightsail coverage](aws-lightsail-resources.md) |

The current AWS adapter is implemented in
`apps/console/cloud/aws/models.py`. The Lightsail adapter is implemented in
`apps/console/cloud/aws/lightsail.py`. Status checks are resolved dynamically
from the provider and asset type.

## Read-only account discovery

The credential validated successfully with STS. `ec2:DescribeRegions` returned
34 enabled Regions. Representative read-only calls in `us-east-1` and
`ap-southeast-1`, plus global-service calls, successfully reached the control
planes for:

- EC2/VPC, Auto Scaling, RDS, Lambda, DynamoDB, ELB/ELBv2, ECS, ECR, EKS,
  ElastiCache, MemoryDB, OpenSearch, App Runner, and Lightsail;
- CloudWatch alarms, CloudWatch Logs, SNS, SQS, API Gateway, EventBridge, and
  Step Functions;
- S3, Route 53, CloudFront, ACM, WAF, Global Accelerator, CloudFormation, and
  the Resource Groups Tagging API; and
- AWS Backup, Secrets Manager, SSM Parameter Store, KMS, CloudTrail, Athena,
  Cost Explorer anomaly detection, and X-Ray.

Most of the representative regions were empty for workload resources. That is
not evidence that the account is globally empty: AWS resources are regional,
and a complete inventory must enumerate every enabled Region and each global
service separately.

AWS Resource Explorer was also readable: the account returned two indexes, one
view, and a configured default view. Its read-only search surfaced metadata
families including EC2 networking, App Runner, Athena, Cost Explorer anomaly
detection, EventBridge, ElastiCache, MemoryDB, X-Ray, KMS, IAM, and Resource
Explorer itself. Resource Explorer is useful as a discovery accelerator, but
service-native list/describe/get calls must remain authoritative because result
completeness depends on the index and permissions. See the [Resource Explorer
API reference](https://docs.aws.amazon.com/resource-explorer/latest/apireference/Welcome.html)
and AWS guidance on [full versus partial resource results](https://docs.aws.amazon.com/resource-explorer/latest/userguide/manage-service-check.html).

Because AWS can perform account-managed Resource Explorer initialization for a
first-time principal in some configurations, CloudTrail was checked after the
probe for `CreateIndex`, `CreateView`, `AssociateDefaultView`, `UpdateIndexType`,
and `UpdateView` events from the last two hours. None were present.

The Resource Groups Tagging API returned only metadata for existing tagged
resources in the probed Region, including the account's default EC2 network
objects and existing Lightsail resources. Those resources were not used for
tests and were not modified. The Tagging API is not a replacement for native
inventory: AWS documents that `GetResources` can query resource types, while
tagging support varies by service; see the [Tagging API reference](https://docs.aws.amazon.com/resourcegroupstagging/latest/APIReference/making-api-requests.html).

## Recommended integration roadmap

### Priority 0: enterprise inventory and failure detection — complete

| Resource family | What CloudMoo should add | Read-only boundary |
| --- | --- | --- |
| Multi-Region inventory | Resource Explorer/Tagging API as a discovery hint, followed by native service inventory in every enabled Region | `Search`, `GetResources`, `List`, `Describe`, and `Get` only; never create indexes/views as part of sync |
| VPC and EC2 networking | VPCs, subnets, route tables, internet gateways, NAT gateways, network ACLs, ENIs, VPC peering, Transit Gateway attachments, VPN connections, and flow logs | Report state/configuration and relationships; do not modify routes, ACLs, gateways, or security groups |
| Auto Scaling and EC2 dependencies | Auto Scaling groups, launch templates, AMIs, instance status checks, placement, and EBS attachments | Inventory and CloudWatch-backed health only; no scaling, reboot, or replacement actions |
| CloudWatch and Logs | Alarms, composite alarms, metric streams/metrics, log groups, retention, and selected operational signals | Do not ingest arbitrary log bodies by default; bound size/time, redact secrets, and never change retention or alarms |
| Containers and Kubernetes | ECR repositories/images/scanning, ECS task definitions/deployments/events, EKS clusters/node groups/add-ons/Fargate profiles, App Runner services/deployments | No image push, deployment, service update, cluster change, or log deletion |
| DNS and edge | Route 53 hosted zones/records, CloudFront distributions/policies, WAFv2 Web ACLs/rules, Global Accelerator, and regional ACM certificates | Inventory and expiry/health checks only; no DNS changes, invalidations, certificate requests, or WAF rule updates |
| Backups | AWS Backup vaults, plans, selections, jobs, recovery points, copy jobs, and retention metadata, alongside EBS/RDS snapshots | List and inspect only; no backup, restore, retention, vault, or recovery-point mutations |

The Priority 0 integration is now implemented. It wires the following
read-only adapters into Django persistence, AWS account synchronization,
monitoring dispatch, inventory/detail UI, admin registration, migrations, and
the IAM policy:

- `apps/console/cloud/aws/network.py` — VPC and EC2 networking/dependencies;
- `apps/console/cloud/aws/observability.py` — CloudWatch alarms/metrics and Logs;
- `apps/console/cloud/aws/containers.py` — ECR, ECS definitions/deployments, EKS,
  and App Runner;
- `apps/console/cloud/aws/edge.py` — Route 53, CloudFront, WAF, Global
  Accelerator, and regional ACM certificates; and
- `apps/console/cloud/aws/backup.py` — AWS Backup resources plus regional EBS
  and RDS snapshots.

The integration tests are in `tests/test_aws_priority0_integration.py` and are
credential-free. They cover model registration and asset values, sync
orchestration, regional ACM/snapshot context, monitoring dispatch, and
read-only policy assertions. The provider-focused suites remain
`tests/test_aws_discovery.py`, `tests/test_aws_network.py`,
`tests/test_aws_observability.py`, `tests/test_aws_containers.py`,
`tests/test_aws_edge.py`, and `tests/test_aws_backup.py`.

No live AWS lifecycle tests are performed by this code change. In particular,
it does not create, update, deploy, start, stop, attach, detach, restore, or
delete AWS resources.

### Priority 1: platform and data services

Implemented with read-only regional inventory and status checks:

- **Databases and storage:** Aurora/RDS clusters, ElastiCache clusters and
  replication/serverless caches, MemoryDB clusters, OpenSearch domains, EFS
  file systems, and FSx file systems. Existing Priority 0 snapshot coverage
  continues to cover EBS/RDS snapshots.
- **Application integration:** API Gateway REST and v2 APIs, EventBridge
  buses/rules/schedules/pipes, SNS topics, SQS queues, Step Functions state
  machines, Athena workgroups/catalogs, and CloudFormation stacks.
- **Delivery and hosting:** Elastic Beanstalk applications/environments,
  CodeBuild projects and bounded recent builds, and CodePipeline pipelines and
  bounded recent executions. App Runner remains covered by the Priority 0
  container adapter; CodeBuild/CodePipeline provide the deployment-event
  surface without invoking a deployment service.

The adapters are implemented in `apps/console/cloud/aws/data_services.py`,
`apps/console/cloud/aws/application_services.py`, and
`apps/console/cloud/aws/delivery.py`. They use bounded allowlisted metadata,
stable Region-scoped IDs, fail-closed reconciliation, and read-only monitoring
calls. Priority 1 is now wired into account sync, model registration,
monitoring dispatch, inventory/detail UI, admin, migrations, and the read-only
IAM policy.

### Priority 2: security, governance, and FinOps

Implemented with read-only inventory, bounded metadata, and status checks:

- **Security and governance:** IAM users, roles, and local policies; KMS keys
  and aliases; CloudTrail trails; AWS Config rules and recorders; GuardDuty
  detectors; Security Hub; Inspector; Macie; and Firewall Manager policies.
  IAM is collected once with a `global` identity; the remaining families are
  enumerated per enabled Region.
- **Credential/configuration posture:** Secrets Manager secret metadata and SSM
  Parameter Store parameter metadata, including safe tags and rotation or
  last-changed posture where the provider exposes it. The adapters never call
  `GetSecretValue`, `GetParameter`, or any equivalent value API, and never
  persist secret values, parameter values, Lambda environment values, or
  private key material. Credentials are supplied only as ephemeral checker
  context.
- **Account operations and FinOps:** AWS Health events, Trusted Advisor
  checks, Cost Explorer usage/cost signals, Cost Anomaly Detection monitors
  and subscriptions, and bounded anomaly findings. These are account-scoped
  control-plane assets, persisted with `scope=account` and
  `control_plane_region=us-east-1` for endpoint and audit context; they are not
  fanned out as regional workload resources.

The three provider lanes are implemented in
`apps/console/cloud/aws/security_governance.py`,
`apps/console/cloud/aws/credentials_config.py`, and
`apps/console/cloud/aws/account_operations.py`. Priority 2 is wired into
Django model registration, account synchronization, monitoring dispatch,
asset detail/list lookup, generic dashboard counts, admin registration, the
read-only IAM policy, and migration state. Unsupported asset types continue
to fail closed in the monitoring dispatcher.

## Enterprise implementation requirements

Every future adapter should preserve these invariants:

1. Inventory and monitoring use an account identifier, Region, provider type,
   and stable resource ID together; names alone are not identity.
2. Pagination, retries, throttling, and incomplete-page failures are explicit.
   A partial provider response must not mark all unseen local resources as
   deleted.
3. Regional services are enumerated across every enabled Region. Global
   services such as Route 53 and CloudFront have dedicated global handling;
   ACM is regional and must not be queried only in one Region.
4. The status model distinguishes `available`, `degraded`, `pending`,
   `failed`, `stopped`, `expired`, `missing`, and provider/API errors instead
   of collapsing all non-success responses into one state.
5. Provider payloads are allowlisted before persistence. Logs and metadata are
   bounded and passed through the shared sensitive-value redaction path.
6. Recommended IAM policies contain only the required read actions. No
   `Create*`, `Put*`, `Update*`, `Delete*`, `Start*`, `Stop*`, `Attach*`,
   `Detach*`, `Allocate*`, `Release*`, `Restore*`, or `Modify*` actions belong
   in an inventory/monitoring credential.
7. Live lifecycle tests use a unique test prefix and mandatory owner/tag
   identity. Every mutating request must be allowlisted to a resource ID
   created by that test run; existing resources are never valid test targets.

## Read-only policy

`aws-cloudmoo-readonly-policy.json` includes the Priority 0 EC2/VPC, Auto
Scaling, RDS, CloudWatch/Logs, ECR/ECS/EKS/App Runner, Route 53, CloudFront,
WAF, Global Accelerator, ACM, Backup, and STS read actions, plus the Priority
1 database/storage, application-integration, Elastic Beanstalk, CodeBuild,
and CodePipeline reads. It also contains only the exact read actions used by
the Priority 2 adapters and checkers for IAM, KMS, CloudTrail, Config,
GuardDuty, Security Hub, Inspector, Macie, Firewall Manager, Secrets Manager,
SSM, Health, Trusted Advisor, Cost Explorer, and Cost Anomaly Detection. It
does not grant secret or parameter-value reads and contains no lifecycle
mutation actions.

## Testing status

The local integration and provider-focused tests use mocks and fixtures,
including read-only-operation guards, malformed-response handling, regional
identity, redaction, and reconciliation safety. No live AWS lifecycle tests or
resource creation are part of this implementation. The Priority 2 integration
suite is `tests/test_aws_priority2_integration.py`; it is credential-free and
asserts model/type registration, concrete relations, sync orchestration,
monitoring ownership, exact policy reads, forbidden mutations, and the
secret/parameter-value boundary. No live AWS testing is claimed or required
for these checks.
