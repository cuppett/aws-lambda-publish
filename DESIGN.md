# aws-lambda-publish — Design

Status: Implemented and Deployed

This document describes the architecture, components, data model, flows, and operational details for the aws-lambda-publish project. The system's purpose is to detect ECR image tag updates and update Lambda container image pointers either directly or via a CodePipeline + CodeDeploy deployment flow. The controller runs in a hub (regional) account and can assume roles into spoke accounts/regions.

## Goals

- Detect ECR image tag changes (PUSH) and resolve them to an immutable image digest.
- Look up subscriptions for repo:tag in DynamoDB.
- For each subscription (target), perform either:
  - Direct update: Update Lambda to new image (publish version and optionally update alias), or
  - Pipeline deploy: Start a generic pipeline execution that publishes and triggers a CodeDeploy deployment (traffic shifting, rollbacks).
- Support cross-account and cross-region deployments via assume-role.
- Scale to many targets per event (fan-out) while being safe (idempotent, bounded concurrency).
- Provide monitoring, logging, retry/backoff, and DLQ for failures.

## High-level architecture

- ECR -> EventBridge (region) rule for Image Action PUSH
- Controller Lambda (hub region) triggered by EventBridge
  - Resolves tag -> digest (ECR DescribeImages)
  - Queries DynamoDB table for subscriptions
  - For each subscription: assume role (if provided) and either perform direct update or start a pipeline
- DynamoDB: mapping table of subscriptions (PK=REG#...#REPO#...#TAG#...; SK=TARGET#...)
- Pipeline (optional per account/region): generic CodePipeline + CodeBuild + CodeDeploy resources with complete buildspec and variable propagation via SSM Parameter Store
- Spoke roles (deployed in target accounts): roles the Controller assumes to act remotely with ECR access and SSM parameter permissions
- Monitor Lambda (scheduled): polls pending pipeline/deploy executions for status and writes back (runs every 5 minutes)

## Components and responsibilities

- Controller Lambda (primary): orchestrates detection -> lookup -> update or pipeline start; concurrent worker model; idempotency and retry. Includes comprehensive error handling, CloudWatch metrics, and structured logging.
- ECR client: resolves tag to digest, picks latest image by pushedAt, respects registryId, retries on throttling.
- DynamoDB client: query subscriptions for a repo:tag; robust marshalling/unmarshalling (S, N, BOOL, M, L) and helpers to record idempotency/status.
- STS client: assume roles into spoke accounts/regions; uses ASSUME_ROLE_SESSION_NAME.
- Lambda client (spoke): enhanced GetFunction/GetFunctionConfiguration, UpdateFunctionCode with timeout handling, PublishVersion, UpdateAlias/CreateAlias with multiple update strategies.
- Pipeline client: start pipeline executions with SSM Parameter Store variable propagation (parameter_store strategy) and legacy clientRequestToken correlation.
- Monitor Lambda: fully implemented polling and status updates for pipeline/CodeDeploy executions with DynamoDB status tracking.
- Metrics client: CloudWatch metrics emission for monitoring function updates, pipeline starts, errors, and performance.

## DynamoDB schema

Table: ImageTagSubscriptions
- PK (HASH): string — REG#{registryId}#REPO#{repositoryName}#TAG#{imageTag}
- SK (RANGE): string — TARGET#{region}#{accountId}#{functionName}
- Attributes:
  - mode: string — "direct" | "pipeline"
  - target: map — { accountId, region, functionName, aliasName? }
  - codeDeploy: map (optional) — { applicationName, deploymentGroupName, deploymentConfigName }
  - pipeline: map (optional) — { name }
  - assumeRoleArn: string (optional) — role to assume in target account
  - pipelineAssumeRoleArn: string (optional) — role to assume to start pipeline in target account
  - lastProcessedDigest: string (optional) — last digest acted on (idempotency)
  - lastExecutionId: string (optional) — pipeline execution id or deployment id
  - lastStatus: string (optional) — status

Example direct item:

{
  "PK": "REG#123456789012#REPO#orders#TAG#prod",
  "SK": "TARGET#us-east-1#111111111111#orders-fn",
  "mode": "direct",
  "target": { "accountId": "111111111111", "region": "us-east-1", "functionName": "orders-fn", "aliasName": "prod" },
  "assumeRoleArn": "arn:aws:iam::111111111111:role/HubDeploymentRole"
}

Example pipeline item:

{
  "PK": "REG#123456789012#REPO#orders#TAG#prod",
  "SK": "TARGET#eu-west-1#222222222222#orders-fn",
  "mode": "pipeline",
  "target": { "accountId": "222222222222", "region": "eu-west-1", "functionName": "orders-fn", "aliasName": "prod" },
  "codeDeploy": { "applicationName": "Orders", "deploymentGroupName": "Prod", "deploymentConfigName": "Canary10Percent5Minutes" },
  "pipeline": { "name": "LambdaGenericDeploy" },
  "pipelineAssumeRoleArn": "arn:aws:iam::222222222222:role/HubPipelineStarter"
}

## Controller Lambda behavior

Environment variables:
- TABLE_NAME
- DEFAULT_MODE (direct|pipeline)
- DEFAULT_UPDATE_STRATEGY (publish-and-alias)
- MAX_PARALLEL_TARGETS
- LOG_LEVEL
- METRICS_NAMESPACE
- ASSUME_ROLE_SESSION_NAME

Steps per invocation:
1. Validate EventBridge event: extract registryId, repositoryName, imageTag.
2. Resolve tag to digest via ECR DescribeImages. Choose most recent by imagePushedAt.
3. Compute PK and query DynamoDB for subscription items.
4. For each target item (parallel up to MAX_PARALLEL_TARGETS):
   - Optionally assume role (assumeRoleArn) via STS.
   - If mode == direct:
     - Use Lambda client in target region with assumed creds.
     - GetFunctionConfiguration and parse current ImageUri digest.
     - If digest differs, call UpdateFunctionCode(ImageUri=repo@digest) and wait for LastUpdateStatus==Successful.
     - PublishVersion, then if aliasName provided and strategy requires, UpdateAlias/CreateAlias to new version.
     - Record lastProcessedDigest and lastStatus in DynamoDB.
   - If mode == pipeline:
     - Assume pipelineAssumeRoleArn in target account (if provided) or use current credentials.
     - Start pipeline execution with variables: IMAGE_URI, FUNCTION_NAME, ALIAS_NAME, DEPLOY_APP, DEPLOY_GROUP, DEPLOY_CONFIG stored in SSM Parameter Store.
     - Record executionId in DynamoDB for monitoring.
5. Log outcomes with structured JSON logging, publish CloudWatch metrics (UpdatedFunctionCount, NoOpCount, Failures, ProcessingDurationSeconds), handle errors per-target; failures do not block other targets.
6. Return comprehensive results including target count and processing time.

Idempotency:
- Before performing an update, perform a conditional update (SET lastProcessedDigest = :d IF attribute_not_exists OR <> :d). If condition fails, skip work (noop-idempotent).
- After success, update lastProcessedDigest and lastStatus.

Retries and throttling:
- Use exponential backoff for select operations (ECR digest lookup, per-target operations wrapper); expand to jittered retries in future.
- Limit per-invocation concurrency via ThreadPoolExecutor (MAX_PARALLEL_TARGETS).

Security and IAM:
- Controller role needs least privilege for ECR DescribeImages, DynamoDB Query/Update, lambda:Get/Update/Publish, codepipeline:StartPipelineExecution, sts:AssumeRole, logs, cloudwatch:PutMetricData.
- Spoke accounts must deploy roles that trust the hub account or a specific TrustedHubRoleArn; policies should be scoped (current templates are permissive and to be tightened).
- Use resource scoping in IAM policies where possible.

## Direct update sequence

1. Controller resolves digest: {account}.dkr.ecr.{region}.amazonaws.com/{repo}@{sha256}
2. Controller assumes DeploymentRole in target account/region.
3. Controller checks current ImageUri; if digest differs:
   - UpdateFunctionCode(ImageUri=newImageUri)
   - Wait for LastUpdateStatus == Successful
   - PublishVersion
   - If alias configured: update alias to new version or create alias
4. Update DynamoDB lastProcessedDigest and lastStatus.

Notes:
- UpdateFunctionCode with ImageUri requires that the target account has access to the ECR image. If cross-account ECR, ensure repository policy allows the target account to pull.

## Pipeline / CodeDeploy sequence

1. Controller either updates function code and publishes version (Option A) or the pipeline's build step does it (Option B).
2. Controller starts pipeline execution in the target account/region (assumes pipeline starter role if necessary) with variables including TargetVersion or IMAGE_URI.
3. Pipeline performs CodeBuild step (if configured) to call UpdateFunctionCode + PublishVersion and emit appspec.json.
4. Pipeline deploy stage uses CodeDeploy (Lambda) to shift alias traffic from CurrentVersion to TargetVersion according to deployment config (canary, linear, all-at-once).
5. CodeDeploy monitors alarms and can automatically rollback on failures. Monitor Lambda polls pipeline/deploy status and updates DynamoDB.

When to choose:
- Use pipeline mode for controlled traffic shifting, built-in rollback, and hooks.
- Use direct mode for faster immediate updates where traffic shift is not required.

## Cross-account & cross-region

- Controller uses STS:AssumeRole to obtain temporary credentials for actions in spoke accounts.
- Two role types in spoken accounts:
  - DeploymentRole: allows lambda update/publish/alias in the spoke account.
  - PipelineStarterRole: allows StartPipelineExecution in the spoke account.
- For pipeline workloads that must run in the target account, deploy the generic pipeline stack there so the pipeline executes entirely in the target account.

## CloudFormation / SAM layout

Stacks:
- Hub/core (SAM): ControllerFunction, MonitorFunction, DynamoDB table, EventBridge rules, IAM roles with SSM Parameter Store permissions. SAM template (template.yaml) is primary deployment path.
- Pipeline (per account/region): CodePipeline with S3 artifact store, CodeBuild with complete buildspec for Lambda updates, CodeDeploy application & deployment groups, roles with SSM permissions.
- Spoke roles (per target account): DeploymentRole with ECR and Lambda permissions, PipelineStarterRole with SSM access, MonitorRole with pipeline status permissions.

Deployment order:
1. Deploy spoke roles in each target account.
2. Deploy pipeline stack in accounts/regions where pipeline will run.
3. Deploy hub/core stack.
4. Populate DynamoDB subscription items.

## Observability

- CloudWatch Logs for Controller and Monitor lambdas with structured JSON logging.
- Metrics (CloudWatch): UpdatedFunctionCount, NoOpCount, Failures, PipelineStartCount, ProcessingDurationSeconds, TargetsProcessed by Repository/Tag/Mode with dimensions.
- Comprehensive error tracking with TargetProcessingErrors, DigestResolutionFailures, InvalidEvents metrics.
- Structured JSON logs with correlationId (event id), target details, processing times, and error context.

## Testing

- Unit tests (pytest + moto) included: ECR digest, DynamoDB marshalling/idempotency, Lambda direct update flow via stubber.
- End-to-end testing: Complete circuit tested with real ECR repository, Lambda function, and DynamoDB subscriptions in AWS account 771294529343/us-west-2.
- Build pipeline: SAM build/deploy tested and working with Python 3.13 runtime.

## Operational runbook (brief)

- Add subscription: PutItem into DynamoDB with PK/ SK and target details.
- Remove subscription: DeleteItem from DynamoDB.
- For testing: push a new image tag to ECR and verify controller logs show updates.
- Rollback: use CodeDeploy rollback or update alias back to previous version.

## Scaling considerations

- Limit per-invocation concurrency using MAX_PARALLEL_TARGETS.
- For very large fan-outs, use SQS to queue target operations and have a worker Lambda scale independently.
- Ensure spoke roles and pipelines can handle concurrent operations.

## Security considerations

- Minimize IAM resource wildcards; scope to target function ARNs and pipeline names where possible.
- Ensure ECR repository policies permit pulls from target accounts if image registry is cross-account.
- Rotate any long-lived credentials; prefer cross-account assume-role with short sessions.

## Implementation Status: COMPLETE ✅

All major components have been implemented and tested:

✅ **Completed:**
- Full Monitor Lambda polling of CodePipeline/CodeDeploy and DDB status updates
- Enhanced pipeline template with artifacts, buildspec, appspec, and SSM Parameter Store variable propagation
- CloudWatch metrics emission from controller/monitor with comprehensive error tracking
- Enhanced IAM permissions for ECR, SSM Parameter Store, and cross-account access
- Unit tests with pytest + moto and end-to-end testing in AWS
- SAM build/deploy pipeline with Python 3.13 runtime

## Future Enhancements

- Add DLQ wiring and SNS alerts for persistent failures
- Further tighten IAM resource scoping to specific function ARNs and pipeline names  
- Add CI workflow for automated lint/tests/build and integration tests
- Add support for CodeDeploy alarm-based rollbacks
- Implement parameter cleanup for old pipeline executions


---

This DESIGN.md aims to be the canonical reference for implementing and operating the aws-lambda-publish project. Update as implementation details change.