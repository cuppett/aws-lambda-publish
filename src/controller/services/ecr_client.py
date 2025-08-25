import boto3
import botocore
import time
import logging

logger = logging.getLogger(__name__)


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

    def check_vulnerabilities(self, repository, digest, registry_id=None, threshold='HIGH'):
        params = {"repositoryName": repository, "imageId": {"imageDigest": digest}}
        if registry_id:
            params["registryId"] = registry_id
        
        severity_order = ['INFORMATIONAL', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL', 'UNDEFINED']
        try:
            threshold_index = severity_order.index(threshold.upper())
        except ValueError:
            logger.error(f"Invalid severity threshold: {threshold}")
            return True

        try:
            paginator = self.client.get_paginator('describe_image_scan_findings')
            for page in paginator.paginate(**params):
                if page['imageScanStatus']['status'] == 'FAILED':
                    logger.warning(f"Image scan failed for {repository}@{digest}: {page['imageScanStatus'].get('description')}")
                    return False 

                if page['imageScanStatus']['status'] != 'COMPLETE':
                    logger.info(f"Image scan not complete for {repository}@{digest}, status is {page['imageScanStatus']['status']}")
                    return False

                findings = page.get('imageScanFindings', {}).get('findings', [])
                for finding in findings:
                    severity = finding.get('severity', 'UNDEFINED')
                    try:
                        severity_index = severity_order.index(severity)
                        if severity_index >= threshold_index:
                            logger.warning(f"Vulnerability found for {repository}@{digest} with severity {severity} >= {threshold}")
                            return True
                    except ValueError:
                        logger.warning(f"Unknown severity '{severity}' found for {repository}@{digest}")

            return False
        except botocore.exceptions.ClientError as e:
            if e.response.get('Error', {}).get('Code') == 'ScanNotFoundException':
                logger.info(f"No scan found for image {repository}@{digest}")
                return False
            logger.error(f"Error checking vulnerabilities for {repository}@{digest}: {e}")
            return True
        except Exception as e:
            logger.error(f"Unexpected error checking vulnerabilities for {repository}@{digest}: {e}")
            return True
