import boto3
import logging

logger = logging.getLogger(__name__)


class PipelineClient:
    def __init__(self, region=None, credentials=None):
        session = boto3.Session(region_name=region,
                                aws_access_key_id=(credentials.get('AccessKeyId') if credentials else None),
                                aws_secret_access_key=(credentials.get('SecretAccessKey') if credentials else None),
                                aws_session_token=(credentials.get('SessionToken') if credentials else None))
        self.client = session.client('codepipeline')

    def start_pipeline(self, name, variables: dict):
        try:
            # Note: CodePipeline's StartPipelineExecution API does not accept variables directly in classic pipelines.
            # Projects commonly encode variables via parameter store or artifact input; here we pass clientRequestToken to correlate.
            token = None
            try:
                token = variables.get('IMAGE_URI', '')
            except Exception:
                token = None
            kwargs = {"name": name}
            if token:
                kwargs["clientRequestToken"] = token[:128]
            resp = self.client.start_pipeline_execution(**kwargs)
            return {"pipeline": name, "status": "started", "executionId": resp.get('pipelineExecutionId')}
        except Exception as e:
            logger.exception("Failed start pipeline")
            return {"pipeline": name, "status": "error", "error": str(e)}
