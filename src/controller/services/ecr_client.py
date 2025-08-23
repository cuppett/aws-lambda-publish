import boto3
import botocore
import time


class ECRClient:
    def __init__(self, region=None, credentials=None):
        session = boto3.Session(region_name=region,
                                aws_access_key_id=(credentials.get('AccessKeyId') if credentials else None),
                                aws_secret_access_key=(credentials.get('SecretAccessKey') if credentials else None),
                                aws_session_token=(credentials.get('SessionToken') if credentials else None))
        self.client = session.client('ecr')

    def get_digest(self, repository, tag, registry_id=None):
        params = {"repositoryName": repository, "imageIds": [{"imageTag": tag}]}
        if registry_id:
            params["registryId"] = registry_id
        for i in range(3):
            try:
                resp = self.client.describe_images(**params)
                images = resp.get('imageDetails', [])
                if not images:
                    return None
                images.sort(key=lambda x: x.get('imagePushedAt') or 0, reverse=True)
                image = images[0]
                return image.get('imageDigest')
            except botocore.exceptions.ClientError as e:
                if e.response.get('Error', {}).get('Code') in ('ThrottlingException', 'TooManyRequestsException') and i < 2:
                    time.sleep(0.5 * (2 ** i))
                    continue
                return None
            except Exception:
                return None
