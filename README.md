# aws-lambda-publish

Controller that detects ECR tag pushes and updates Lambda container image functions (direct) or triggers a generic CodePipeline/CodeDeploy workflow (pipeline).

## Structure
- src/controller - controller lambda
- src/monitor - monitor lambda (pipeline mode status polling)
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

## Error alerts

The stack adds CloudWatch Logs metric filters on the controller and monitor log
groups (`[ERROR]` lines and Lambda timeouts) with alarms
`lambda-publish-core-controller-errors` / `-monitor-errors`. Set the
`AlertTopicArn` parameter to an SNS topic to be notified (default
`arn:aws:sns:us-east-1:771294529343:alert_steve`; empty string disables
notifications).
