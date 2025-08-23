import boto3
import time
import logging

logger = logging.getLogger(__name__)

class LambdaClient:
    def __init__(self, region=None, credentials=None):
        session = boto3.Session(region_name=region,
                                aws_access_key_id=(credentials.get('AccessKeyId') if credentials else None),
                                aws_secret_access_key=(credentials.get('SecretAccessKey') if credentials else None),
                                aws_session_token=(credentials.get('SessionToken') if credentials else None))
        self.client = session.client('lambda')

    def get_current_image_digest(self, function_name):
        try:
            # Try GetFunctionConfiguration first
            cfg = self.client.get_function_configuration(FunctionName=function_name)
            
            # Check if it's a container image function
            if cfg.get('PackageType') != 'Image':
                logger.debug(f"Function {function_name} is not a container image function")
                return None
            
            # Try to get ImageUri from Code field
            uri = cfg.get('Code', {}).get('ImageUri') if isinstance(cfg.get('Code'), dict) else None
            
            if not uri:
                # Fall back to GetFunction if ImageUri not in configuration
                try:
                    func_resp = self.client.get_function(FunctionName=function_name)
                    uri = func_resp.get('Code', {}).get('ImageUri')
                except Exception as e:
                    logger.warning(f"Failed to get function details for {function_name}: {e}")
                    return None
            
            if uri and '@' in uri:
                digest = uri.split('@', 1)[1]
                logger.debug(f"Current digest for {function_name}: {digest}")
                return digest
            elif uri and ':' in uri:
                # Handle tag-based URIs (though Lambda should use digest)
                logger.warning(f"Function {function_name} uses tag-based URI: {uri}")
                return None
            else:
                logger.warning(f"Unexpected ImageUri format for {function_name}: {uri}")
                return None
                
        except self.client.exceptions.ResourceNotFoundException:
            logger.error(f"Function {function_name} not found")
            return None
        except Exception as e:
            logger.exception(f"Failed to get current image digest for {function_name}: {e}")
            return None

    def update_function_direct(self, function_name, image_uri, alias_name=None, update_strategy="publish-and-alias"):
        try:
            cur = self.get_current_image_digest(function_name)
            
            if '@' not in image_uri:
                logger.error(f"Invalid image URI format (missing digest): {image_uri}")
                return {"function": function_name, "status": "error", "error": "Invalid image URI format"}
            
            new_digest = image_uri.split('@', 1)[1]
            
            # Check if update is needed
            if cur is not None and cur == new_digest:
                logger.info(f"Function {function_name} already at digest {new_digest}")
                return {"function": function_name, "status": "noop", "current_digest": cur}
            
            logger.info(f"Updating function {function_name} from {cur} to {new_digest}")
            
            # Update function code
            resp = self.client.update_function_code(FunctionName=function_name, ImageUri=image_uri, Publish=False)
            
            # Wait for update to complete
            max_wait_time = 300  # 5 minutes
            wait_interval = 2
            waited = 0
            
            while waited < max_wait_time:
                cfg = self.client.get_function_configuration(FunctionName=function_name)
                status = cfg.get('LastUpdateStatus')
                
                if status == 'Successful':
                    logger.info(f"Function {function_name} update completed successfully")
                    break
                elif status == 'Failed':
                    error_reason = cfg.get('LastUpdateStatusReason', 'Unknown error')
                    logger.error(f"Function {function_name} update failed: {error_reason}")
                    return {"function": function_name, "status": "error", "error": f"Update failed: {error_reason}"}
                
                time.sleep(wait_interval)
                waited += wait_interval
            else:
                logger.error(f"Function {function_name} update timed out after {max_wait_time} seconds")
                return {"function": function_name, "status": "error", "error": "Update timeout"}
            
            # Handle different update strategies
            version = None
            
            if update_strategy in ("publish-and-alias", "publish-only"):
                # Publish new version
                logger.info(f"Publishing new version for function {function_name}")
                publish = self.client.publish_version(FunctionName=function_name)
                version = publish.get('Version')
                logger.info(f"Published version {version} for function {function_name}")
            
            if update_strategy == "publish-and-alias" and alias_name:
                # Update or create alias
                try:
                    logger.info(f"Updating alias {alias_name} to version {version} for function {function_name}")
                    self.client.update_alias(
                        FunctionName=function_name, 
                        Name=alias_name, 
                        FunctionVersion=version
                    )
                    logger.info(f"Updated alias {alias_name} to version {version}")
                except self.client.exceptions.ResourceNotFoundException:
                    logger.info(f"Creating new alias {alias_name} for version {version}")
                    self.client.create_alias(
                        FunctionName=function_name, 
                        Name=alias_name, 
                        FunctionVersion=version
                    )
                    logger.info(f"Created alias {alias_name} for version {version}")
            
            result = {
                "function": function_name, 
                "status": "updated", 
                "new_digest": new_digest,
                "previous_digest": cur
            }
            
            if version:
                result["version"] = version
            if alias_name and update_strategy == "publish-and-alias":
                result["alias"] = alias_name
                
            return result
            
        except self.client.exceptions.ResourceNotFoundException:
            logger.error(f"Function {function_name} not found")
            return {"function": function_name, "status": "error", "error": "Function not found"}
        except self.client.exceptions.InvalidParameterValueException as e:
            logger.error(f"Invalid parameter for function {function_name}: {e}")
            return {"function": function_name, "status": "error", "error": f"Invalid parameter: {str(e)}"}
        except Exception as e:
            logger.exception(f"Failed to update function {function_name}")
            return {"function": function_name, "status": "error", "error": str(e)}
