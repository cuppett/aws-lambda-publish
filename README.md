# aws-lambda-publish

Controller that detects ECR tag pushes and updates Lambda container image functions (direct) or triggers a generic CodePipeline/CodeDeploy workflow (pipeline).

## Structure
- src/controller - controller lambda
- src/monitor - monitor lambda (placeholder)
- infra - CloudFormation templates (core, pipeline, spoke roles)
- tests - unit tests (pytest + moto)
- template.yaml - SAM template for controller + table + monitor

## Quick start

Prereqs: AWS SAM CLI, AWS CLI, AWS profile set.

- make install
- make test
- make build
- AWS_PROFILE=<profile> AWS_REGION=<region> make deploy

Then add a subscription item to DynamoDB and push an image to ECR to trigger.

See DESIGN.md for architecture.
