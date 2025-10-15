import boto3
import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class NotificationClient:
    def __init__(self, region=None, credentials=None, sns_topic_arn=None):
        session = boto3.Session(region_name=region,
                                aws_access_key_id=(credentials.get('AccessKeyId') if credentials else None),
                                aws_secret_access_key=(credentials.get('SecretAccessKey') if credentials else None),
                                aws_session_token=(credentials.get('SessionToken') if credentials else None))
        self.sns_client = session.client('sns')
        self.sns_topic_arn = sns_topic_arn

    def send_deployment_failure_notification(self, 
                                           function_name: str, 
                                           failure_type: str, 
                                           error_details: str,
                                           deployment_context: Optional[Dict[str, Any]] = None) -> bool:
        """
        Send a notification about a deployment failure.
        
        Args:
            function_name: The Lambda function that failed to deploy
            failure_type: Type of failure (health_check, update, rollback, etc.)
            error_details: Detailed error message
            deployment_context: Additional context about the deployment
            
        Returns:
            True if notification was sent successfully, False otherwise
        """
        if not self.sns_topic_arn:
            logger.warning("SNS topic ARN not configured, skipping notification")
            return False
        
        try:
            # Build the notification message
            message_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "functionName": function_name,
                "failureType": failure_type,
                "errorDetails": error_details,
                "deploymentContext": deployment_context or {}
            }
            
            # Create a human-readable subject
            subject = f"Lambda Deployment Failure: {function_name} ({failure_type})"
            
            # Create a formatted message body
            message_body = self._format_message(message_data)
            
            logger.info(f"Sending deployment failure notification for {function_name}")
            
            response = self.sns_client.publish(
                TopicArn=self.sns_topic_arn,
                Subject=subject,
                Message=message_body,
                MessageAttributes={
                    'functionName': {
                        'DataType': 'String',
                        'StringValue': function_name
                    },
                    'failureType': {
                        'DataType': 'String',
                        'StringValue': failure_type
                    }
                }
            )
            
            message_id = response.get('MessageId')
            logger.info(f"Notification sent successfully for {function_name}, MessageId: {message_id}")
            return True
            
        except self.sns_client.exceptions.InvalidParameterException as e:
            logger.error(f"Invalid parameter when sending notification for {function_name}: {e}")
            return False
        except self.sns_client.exceptions.AuthorizationErrorException:
            logger.error(f"Authorization error when sending notification for {function_name}")
            return False
        except Exception as e:
            logger.exception(f"Failed to send notification for {function_name}")
            return False

    def send_health_check_failure_notification(self, 
                                             function_name: str, 
                                             version: str,
                                             health_check_config: Dict[str, Any],
                                             error_details: str) -> bool:
        """
        Send a specific notification for health check failures.
        
        Args:
            function_name: The Lambda function that failed health check
            version: The version that failed
            health_check_config: The health check configuration used
            error_details: Details about the health check failure
            
        Returns:
            True if notification was sent successfully, False otherwise
        """
        deployment_context = {
            "version": version,
            "healthCheckConfig": health_check_config,
            "action": "health_check_and_rollback"
        }
        
        return self.send_deployment_failure_notification(
            function_name=function_name,
            failure_type="health_check",
            error_details=error_details,
            deployment_context=deployment_context
        )

    def send_pipeline_failure_notification(self, 
                                         function_name: str,
                                         pipeline_name: str,
                                         execution_id: str,
                                         error_details: str) -> bool:
        """
        Send a notification for CodePipeline deployment failures.
        
        Args:
            function_name: The Lambda function being deployed
            pipeline_name: The CodePipeline name
            execution_id: The pipeline execution ID
            error_details: Details about the pipeline failure
            
        Returns:
            True if notification was sent successfully, False otherwise
        """
        deployment_context = {
            "pipelineName": pipeline_name,
            "executionId": execution_id,
            "action": "pipeline_deployment"
        }
        
        return self.send_deployment_failure_notification(
            function_name=function_name,
            failure_type="pipeline",
            error_details=error_details,
            deployment_context=deployment_context
        )

    def _format_message(self, message_data: Dict[str, Any]) -> str:
        """
        Format the notification message for better readability.
        
        Args:
            message_data: The message data to format
            
        Returns:
            Formatted message string
        """
        lines = [
            "AWS Lambda Deployment Failure Alert",
            "=====================================",
            "",
            f"Function Name: {message_data['functionName']}",
            f"Failure Type: {message_data['failureType']}",
            f"Timestamp: {message_data['timestamp']}",
            "",
            "Error Details:",
            f"{message_data['errorDetails']}",
            ""
        ]
        
        if message_data['deploymentContext']:
            lines.extend([
                "Deployment Context:",
                json.dumps(message_data['deploymentContext'], indent=2),
                ""
            ])
        
        lines.extend([
            "This is an automated notification from the aws-lambda-publish system.",
            "Please investigate the deployment failure and take appropriate action."
        ])
        
        return "\n".join(lines)