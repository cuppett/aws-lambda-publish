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

    def get_architecture(self, function_name):
        try:
            cfg = self.client.get_function_configuration(FunctionName=function_name)
            archs = cfg.get('Architectures') or []
            return archs[0] if archs else None
        except Exception as e:
            logger.warning(f"Failed to get architecture for {function_name}: {e}")
            return None

    @staticmethod
    def resolve_image_tag(image_tag, architecture):
        if not architecture or not image_tag:
            return image_tag
        if image_tag.endswith(('-amd64', '-aarch64')):
            return image_tag
        suffix = 'aarch64' if architecture == 'arm64' else 'amd64'
        return f"{image_tag}-{suffix}"

    def resolve_target_image(self, function_name, repository, image_tag, registry_id, region):
        architecture = self.get_architecture(function_name)
        resolved_tag = self.resolve_image_tag(image_tag, architecture)
        from .ecr_client import ECRClient
        ecr_client = ECRClient(region=region)
        digest = ecr_client.get_digest(repository, resolved_tag, registry_id)
        image_uri = f"{registry_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{resolved_tag}"
        return image_uri, digest, resolved_tag, architecture

    def _get_image_uri(self, function_name):
        try:
            cfg = self.client.get_function_configuration(FunctionName=function_name)
            if cfg.get('PackageType') != 'Image':
                return None
            uri = cfg.get('Code', {}).get('ImageUri') if isinstance(cfg.get('Code'), dict) else None
            if uri:
                return uri
            func_resp = self.client.get_function(FunctionName=function_name)
            return func_resp.get('Code', {}).get('ImageUri')
        except Exception as e:
            logger.warning(f"Failed to get image URI for {function_name}: {e}")
            return None

    def get_current_image_digest(self, function_name):
        try:
            uri = self._get_image_uri(function_name)
            if not uri:
                return None

            if '@' in uri:
                digest = uri.split('@', 1)[1]
                logger.debug(f"Current digest for {function_name}: {digest}")
                return digest
            elif ':' in uri:
                # Handle tag-based URIs by resolving via ECR
                logger.info(f"Function {function_name} uses tag-based URI: {uri}")
                try:
                    # Parse the URI to extract ECR details
                    # Format: account.dkr.ecr.region.amazonaws.com/repo:tag
                    if '.dkr.ecr.' in uri and '.amazonaws.com/' in uri:
                        parts = uri.split('/')
                        if len(parts) >= 2:
                            host_part = parts[0]  # account.dkr.ecr.region.amazonaws.com
                            repo_and_tag = '/'.join(parts[1:])  # repo:tag
                            
                            if ':' in repo_and_tag:
                                repo, tag = repo_and_tag.rsplit(':', 1)
                                account_id = host_part.split('.')[0]
                                region = host_part.split('.')[3]
                                
                                # Use ECR client to resolve tag to digest
                                from .ecr_client import ECRClient
                                ecr_client = ECRClient(region=region)
                                resolved_digest = ecr_client.get_digest(repo, tag, account_id)
                                if resolved_digest:
                                    logger.debug(f"Resolved tag {tag} to digest {resolved_digest} for {function_name}")
                                    return resolved_digest
                                else:
                                    logger.warning(f"Could not resolve tag {tag} for repo {repo}")
                                    return None
                except Exception as e:
                    logger.warning(f"Failed to resolve tag-based URI for {function_name}: {e}")
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

    def update_function_direct(self, function_name, image_uri, alias_name=None, update_strategy="publish-and-alias", target_digest=None):
        try:
            deployed_uri = self._get_image_uri(function_name)
            tag_based = bool(deployed_uri and '@' not in deployed_uri)

            if '@' in image_uri:
                new_digest = image_uri.split('@', 1)[1]
            elif target_digest:
                new_digest = target_digest
            else:
                logger.error(f"Cannot determine target digest for function {function_name}")
                return {"function": function_name, "status": "error", "error": "Missing target digest"}

            cur = self.get_current_image_digest(function_name)
            
            # Tag-based Lambda URIs do not track ECR tag moves; resolving the tag
            # via ECR always matches the incoming push digest even when Lambda has
            # not pulled the new image yet.
            if cur is not None and cur == new_digest and not tag_based:
                logger.info(f"Function {function_name} already at digest {new_digest}")
                return {"function": function_name, "status": "noop", "current_digest": cur}
            
            logger.info(f"Updating function {function_name} to {image_uri} (digest {new_digest})")
            
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

            if update_strategy in ("code-only", "update-only"):
                return {
                    "function": function_name,
                    "status": "updated",
                    "new_digest": new_digest,
                    "previous_digest": cur,
                }

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
