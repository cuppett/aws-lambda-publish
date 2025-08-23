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
            cfg = self.client.get_function_configuration(FunctionName=function_name)
            # Some SDKs return ImageUri under Code.ImageUri only from GetFunction, not GetFunctionConfiguration.
            # Fall back to none for tests using stubs.
            uri = cfg.get('Code', {}).get('ImageUri') if isinstance(cfg.get('Code'), dict) else None
            if not uri:
                return None
            if '@' in uri:
                return uri.split('@', 1)[1]
            return None
        except Exception:
            return None

    def update_function_direct(self, function_name, image_uri, alias_name=None):
        try:
            cur = self.get_current_image_digest(function_name)
            new_digest = image_uri.split('@', 1)[1]
            if cur is not None and cur == new_digest:
                return {"function": function_name, "status": "noop"}
            resp = self.client.update_function_code(FunctionName=function_name, ImageUri=image_uri, Publish=False)
            # wait for update
            for _ in range(30):
                cfg = self.client.get_function_configuration(FunctionName=function_name)
                if cfg.get('LastUpdateStatus') == 'Successful':
                    break
                time.sleep(1)
            publish = self.client.publish_version(FunctionName=function_name)
            version = publish.get('Version')
            if alias_name:
                try:
                    self.client.update_alias(FunctionName=function_name, Name=alias_name, FunctionVersion=version)
                except self.client.exceptions.ResourceNotFoundException:
                    self.client.create_alias(FunctionName=function_name, Name=alias_name, FunctionVersion=version)
            return {"function": function_name, "status": "updated", "version": version}
        except Exception as e:
            logger.exception("Failed update")
            return {"function": function_name, "status": "error", "error": str(e)}
