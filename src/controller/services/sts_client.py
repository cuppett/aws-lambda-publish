import os
import boto3


class STSClient:
    def __init__(self, region=None):
        self.client = boto3.client('sts')

    def assume_role(self, role_arn, session_name=None):
        name = session_name or os.environ.get('ASSUME_ROLE_SESSION_NAME', 'lambda-publish')
        resp = self.client.assume_role(RoleArn=role_arn, RoleSessionName=name)
        return resp.get('Credentials')
