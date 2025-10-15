import boto3
import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class HealthCheckClient:
    def __init__(self, region=None, credentials=None):
        session = boto3.Session(region_name=region,
                                aws_access_key_id=(credentials.get('AccessKeyId') if credentials else None),
                                aws_secret_access_key=(credentials.get('SecretAccessKey') if credentials else None),
                                aws_session_token=(credentials.get('SessionToken') if credentials else None))
        self.lambda_client = session.client('lambda')

    def perform_health_check(self, function_name: str, version: str, health_check_config: Dict[str, Any]) -> bool:
        """
        Perform a health check on a specific Lambda function version.
        
        Args:
            function_name: The Lambda function name
            version: The specific version to test
            health_check_config: Configuration containing:
                - payload: JSON payload to send to the function
                - timeoutSeconds: Maximum time to wait for response
        
        Returns:
            True if health check passes, False otherwise
        """
        if not health_check_config.get('enabled', False):
            logger.debug(f"Health check disabled for function {function_name}")
            return True
        
        payload = health_check_config.get('payload', '{}')
        timeout_seconds = health_check_config.get('timeoutSeconds', 10)
        
        try:
            # Convert payload string to bytes if it's a string
            if isinstance(payload, str):
                payload_bytes = payload.encode('utf-8')
            else:
                payload_bytes = json.dumps(payload).encode('utf-8')
            
            logger.info(f"Performing health check on {function_name}:{version} with timeout {timeout_seconds}s")
            
            # Invoke the specific version of the function
            function_arn = f"{function_name}:{version}"
            
            response = self.lambda_client.invoke(
                FunctionName=function_arn,
                InvocationType='RequestResponse',
                Payload=payload_bytes
            )
            
            # Check if the invocation was successful
            status_code = response.get('StatusCode', 0)
            
            if status_code != 200:
                logger.error(f"Health check failed for {function_name}:{version} - Status code: {status_code}")
                return False
            
            # Check for function errors
            if 'FunctionError' in response:
                error_type = response['FunctionError']
                logger.error(f"Health check failed for {function_name}:{version} - Function error: {error_type}")
                
                # Log the error payload if available
                if 'Payload' in response:
                    try:
                        error_payload = response['Payload'].read().decode('utf-8')
                        logger.error(f"Error payload: {error_payload}")
                    except Exception as e:
                        logger.warning(f"Could not read error payload: {e}")
                
                return False
            
            # If we get here, the health check passed
            logger.info(f"Health check passed for {function_name}:{version}")
            
            # Optionally log the response payload for debugging
            if 'Payload' in response:
                try:
                    response_payload = response['Payload'].read().decode('utf-8')
                    logger.debug(f"Health check response: {response_payload}")
                except Exception as e:
                    logger.warning(f"Could not read response payload: {e}")
            
            return True
            
        except self.lambda_client.exceptions.ResourceNotFoundException:
            logger.error(f"Function {function_name}:{version} not found during health check")
            return False
        except self.lambda_client.exceptions.InvalidParameterValueException as e:
            logger.error(f"Invalid parameter for health check on {function_name}:{version}: {e}")
            return False
        except self.lambda_client.exceptions.TooManyRequestsException:
            logger.error(f"Rate limited during health check for {function_name}:{version}")
            return False
        except Exception as e:
            logger.exception(f"Health check failed for {function_name}:{version} with unexpected error")
            return False

    def rollback_version(self, function_name: str, failed_version: str) -> bool:
        """
        Delete a failed Lambda version to rollback the deployment.
        
        Args:
            function_name: The Lambda function name
            failed_version: The version to delete
            
        Returns:
            True if rollback was successful, False otherwise
        """
        try:
            # Don't delete $LATEST or numbered versions that might be in use
            if failed_version in ['$LATEST', '1']:
                logger.warning(f"Cannot delete version {failed_version} for {function_name}")
                return False
            
            logger.info(f"Rolling back function {function_name} by deleting version {failed_version}")
            
            self.lambda_client.delete_function(
                FunctionName=function_name,
                Qualifier=failed_version
            )
            
            logger.info(f"Successfully deleted version {failed_version} for function {function_name}")
            return True
            
        except self.lambda_client.exceptions.ResourceNotFoundException:
            logger.warning(f"Version {failed_version} for function {function_name} not found during rollback")
            return True  # Consider this successful since the version is gone
        except self.lambda_client.exceptions.InvalidParameterValueException as e:
            logger.error(f"Invalid parameter during rollback of {function_name}:{failed_version}: {e}")
            return False
        except Exception as e:
            logger.exception(f"Failed to rollback version {failed_version} for function {function_name}")
            return False