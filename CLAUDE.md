# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

aws-lambda-publish is an AWS Lambda-based system that automatically detects ECR image pushes and updates Lambda container functions either directly or via CodePipeline/CodeDeploy workflows. The system supports cross-account deployments and provides comprehensive monitoring.

## Development Commands

### Setup and Testing
```bash
make install          # Install Python dependencies
make test             # Run unit tests with pytest + moto
pytest tests/test_services.py::test_ddb_marshalling_and_idempotency  # Run single test
```

### Build and Deployment  
```bash
make build            # SAM build (requires SAM CLI)
AWS_PROFILE=<profile> AWS_REGION=<region> make deploy  # Deploy to AWS
sam deploy --stack-name lambda-publish-core --capabilities CAPABILITY_NAMED_IAM --region us-west-2 --resolve-s3  # Full deploy command
```

### Infrastructure Deployment
```bash
# Deploy spoke roles in target accounts first
aws cloudformation deploy --template-file infra/spoke-role.yaml --stack-name lambda-publish-spoke-roles --capabilities CAPABILITY_NAMED_IAM --parameter-overrides HubAccountId=<hub-account-id>

# Deploy pipeline infrastructure (optional, for pipeline mode)
aws cloudformation deploy --template-file infra/pipeline.yaml --stack-name lambda-publish-pipeline --capabilities CAPABILITY_NAMED_IAM
```

## Architecture Overview

### Core Components
- **Controller Lambda** (`src/controller/`): Main orchestrator triggered by ECR EventBridge events
- **Monitor Lambda** (`src/monitor/`): Scheduled function that polls pipeline/CodeDeploy status
- **DynamoDB Table**: `ImageTagSubscriptions` stores repo:tag -> Lambda function mappings
- **EventBridge Rule**: Triggers controller on ECR PUSH events

### Key Services Architecture
- **Config** (`src/controller/services/config.py`): Environment variable management with defaults
- **DDBClient**: Handles DynamoDB operations with custom marshalling for complex types (S, N, BOOL, M, L)
- **ECRClient**: Resolves image tags to immutable digests with retry logic
- **LambdaClient**: Updates container functions with multiple strategies (publish-and-alias, publish-only, update-only)
- **PipelineClient**: Starts CodePipeline executions with SSM Parameter Store variable propagation
- **MetricsClient**: CloudWatch metrics emission for monitoring (UpdatedFunctionCount, NoOpCount, Failures, etc.)

### Deployment Modes
1. **Direct Mode**: Controller directly updates Lambda function code and publishes versions
2. **Pipeline Mode**: Controller starts CodePipeline execution which handles CodeDeploy for traffic shifting

### Cross-Account Support
- Controller assumes roles in spoke accounts via STS
- Two role types: DeploymentRole (Lambda updates) and PipelineStarterRole (pipeline execution)
- SSM Parameter Store used for secure variable passing to CodeBuild

## Data Flow

1. ECR image push → EventBridge → Controller Lambda
2. Controller resolves tag to digest via ECR DescribeImages
3. Queries DynamoDB for subscriptions using pattern: `PK=REG#{registryId}#REPO#{repo}#TAG#{tag}`
4. For each target, either:
   - Direct: Update Lambda function immediately with idempotency checks
   - Pipeline: Store variables in SSM Parameter Store and start pipeline execution
5. Monitor Lambda polls pipeline status every 5 minutes and updates DynamoDB

## DynamoDB Schema

```
PK (HASH): REG#{registryId}#REPO#{repositoryName}#TAG#{imageTag}
SK (RANGE): TARGET#{region}#{accountId}#{functionName}
```

Example subscription:
```json
{
  "PK": "REG#123456789012#REPO#myapp#TAG#prod",
  "SK": "TARGET#us-east-1#111111111111#myapp-function",
  "mode": "direct",
  "target": {
    "accountId": "111111111111",
    "region": "us-east-1", 
    "functionName": "myapp-function",
    "aliasName": "prod"
  }
}
```

## Configuration Management

Environment variables are centralized in `Config` class with sensible defaults:
- `TABLE_NAME`: DynamoDB table (default: ImageTagSubscriptions)
- `DEFAULT_MODE`: direct|pipeline (default: direct)  
- `DEFAULT_UPDATE_STRATEGY`: publish-and-alias|publish-only|update-only (default: publish-and-alias)
- `MAX_PARALLEL_TARGETS`: Concurrency limit (default: 10)
- `METRICS_NAMESPACE`: CloudWatch namespace (default: LambdaPublish)

## Testing Strategy

- Unit tests use pytest + moto for AWS service mocking
- DynamoDB marshalling, ECR digest resolution, and Lambda update flows are tested
- Botocore Stubber used for complex Lambda client interactions
- End-to-end testing requires real AWS resources

## Key Implementation Details

### Idempotency
Uses DynamoDB conditional updates: `SET lastProcessedDigest = :d IF attribute_not_exists OR <> :d`

### Pipeline Variable Propagation  
SSM Parameter Store strategy stores variables at `/lambda-publish/pipeline/{name}/{executionId}/` for CodeBuild access

### Error Handling
Comprehensive CloudWatch metrics track failures at component level with structured JSON logging using correlationId

### Concurrent Processing
ThreadPoolExecutor limits parallel target processing to `MAX_PARALLEL_TARGETS` to prevent overwhelming downstream services