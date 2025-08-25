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
- **Monitor Lambda** (`src/monitor/`): Scheduled function (every 5 minutes) that polls pipeline/CodeDeploy status and updates DynamoDB
- **DynamoDB Table**: `ImageTagSubscriptions` stores repo:tag -> Lambda function mappings
- **EventBridge Rules**: Trigger controller on ECR PUSH events, trigger monitor on schedule

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
3. Queries DynamoDB for subscriptions using pattern: `PK={repo}:{tag}`
4. For each target, either:
   - Direct: Update Lambda function immediately with idempotency checks
   - Pipeline: Store variables in SSM Parameter Store and start pipeline execution
5. Monitor Lambda polls pipeline status every 5 minutes and updates DynamoDB

## DynamoDB Schema

```
PK (HASH): {repositoryName}:{imageTag}
SK (RANGE): {accountId}/{region}/{functionName}
```

Example subscription:
```json
{
  "PK": "myapp:prod",
  "SK": "111111111111/us-east-1/myapp-function",
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
- `SCAN_SEVERITY_THRESHOLD`: Blocks deployments if vulnerabilities meet or exceed this level (e.g., HIGH, CRITICAL). Default: `HIGH`.

## Testing Strategy

### Unit Testing
- Unit tests use pytest + moto for AWS service mocking
- DynamoDB marshalling, ECR digest resolution, and Lambda update flows are tested
- Botocore Stubber used for complex Lambda client interactions

### Local Testing Commands
```bash
# Test controller locally with mock events
export AWS_PROFILE=<profile> AWS_REGION=<region> TABLE_NAME=ImageTagSubscriptions
python3 -c "
import sys; sys.path.append('src')
from controller.lambda_handler import handler
# Pass test event to handler function
"

# Test specific service components
python3 -c "
import sys; sys.path.append('src')  
from controller.services.ddb_client import DDBClient
# Test DynamoDB operations directly
"
```

### Live Testing Requirements
- Requires real AWS resources: ECR repository, Lambda functions, DynamoDB table
- Deploy test infrastructure before running end-to-end tests
- Use separate AWS account/region for testing to avoid conflicts

## Key Implementation Details

### Region Handling Critical Issue
**IMPORTANT**: All DynamoDB clients must specify the target region explicitly. The system was initially designed to work across regions, but DynamoDB clients default to us-east-1 if region is not specified. Always pass `region` parameter to `DDBClient()` constructor.

### Lambda URI Resolution
The `LambdaClient.get_current_image_digest()` method handles both:
- Digest-based URIs: `account.dkr.ecr.region.amazonaws.com/repo@sha256:...` 
- Tag-based URIs: `account.dkr.ecr.region.amazonaws.com/repo:tag` (resolved via ECR)

This is critical for idempotency checks when Lambda functions use tag-based ImageURIs instead of digest-based ones.

### ECR Image Scanning
The Controller Lambda can automatically check for vulnerabilities in the pushed image using ECR's image scanning feature. If vulnerabilities are found that meet or exceed the `SCAN_SEVERITY_THRESHOLD`, the deployment is blocked. This requires the `ecr:DescribeImageScanFindings` IAM permission.

### Idempotency
Uses DynamoDB conditional updates: `SET lastProcessedDigest = :d IF attribute_not_exists OR <> :d`

### Pipeline Variable Propagation  
SSM Parameter Store strategy stores variables at `/lambda-publish/pipeline/{name}/{executionId}/` for CodeBuild access

### Error Handling
Comprehensive CloudWatch metrics track failures at component level with structured JSON logging using correlationId

### Concurrent Processing
ThreadPoolExecutor limits parallel target processing to `MAX_PARALLEL_TARGETS` to prevent overwhelming downstream services

## Production Deployment Considerations

### CodeDeploy Deployment Groups
Lambda deployment groups cannot be reliably created via CloudFormation. Create them manually:
```bash
aws deploy create-deployment-group \
  --application-name <app-name> \
  --deployment-group-name <group-name> \
  --service-role-arn <role-arn> \
  --deployment-style '{"deploymentType":"BLUE_GREEN","deploymentOption":"WITH_TRAFFIC_CONTROL"}'
```

### Pipeline Infrastructure
The pipeline template creates a working CodePipeline but requires manual setup of:
- Source stage artifact (the template uses a placeholder S3 source)
- S3 bucket versioning for artifact storage
- Proper buildspec that retrieves variables from SSM Parameter Store

### Monitoring Setup
Monitor Lambda runs every 5 minutes via EventBridge schedule rule. Check CloudWatch Logs group `/aws/lambda/lambda-publish-monitor` for execution details.