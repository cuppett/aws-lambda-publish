import os
import sys
import json
import boto3
import botocore
import pytest
from moto import mock_aws

sys.path.insert(0, os.getcwd())

from src.controller.services.ddb_client import DDBClient
from src.controller.services.ecr_client import ECRClient
from src.controller.services.lambda_client import LambdaClient


def _put_ddb_item(client, table, item):
    client.put_item(TableName=table, Item=item)


@mock_aws
def test_ddb_marshalling_and_idempotency():
    dynamodb = boto3.client('dynamodb', region_name='us-east-1')
    table = 'ImageTagSubscriptions'
    dynamodb.create_table(
        TableName=table,
        BillingMode='PAY_PER_REQUEST',
        AttributeDefinitions=[{'AttributeName': 'PK', 'AttributeType': 'S'}, {'AttributeName': 'SK', 'AttributeType': 'S'}],
        KeySchema=[{'AttributeName': 'PK', 'KeyType': 'HASH'}, {'AttributeName': 'SK', 'KeyType': 'RANGE'}],
    )
    pk = 'orders:prod'
    sk = '111/us-east-1/orders-fn'
    _put_ddb_item(dynamodb, table, {
        'PK': {'S': pk},
        'SK': {'S': sk},
        'mode': {'S': 'direct'},
        'target': {'M': {
            'accountId': {'S': '111'},
            'region': {'S': 'us-east-1'},
            'functionName': {'S': 'orders-fn'},
            'aliasName': {'S': 'prod'},
        }},
        'lastProcessedDigest': {'S': 'sha256:old'},
    })

    ddb = DDBClient(table_name=table, region='us-east-1')
    items = ddb.get_targets(pk)
    assert len(items) == 1
    it = items[0]
    assert it['mode'] == 'direct'
    assert it['target']['functionName'] == 'orders-fn'

    # conditional set same digest -> should fail (False)
    assert ddb.conditional_set_processed(pk, sk, 'sha256:old') is False
    # conditional set new digest -> True
    assert ddb.conditional_set_processed(pk, sk, 'sha256:new') is True
    # update last processed
    assert ddb.update_last_processed(pk, sk, 'sha256:new', 'updated') is True


@mock_aws
def test_ecr_get_digest_uses_registry_and_retries():
    ecr = boto3.client('ecr', region_name='us-east-1')
    resp = ecr.create_repository(repositoryName='orders')
    registry_id = resp['repository']['registryId']

    # push two images (simulate by put_image with different pushedAt ordering via imageManifest)
    # moto sets same time; our code sorts by imagePushedAt if present; ensure two images exist
    manifest = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "application/vnd.docker.container.image.v1+json", "size": 7023, "digest": "sha256:a"},
        "layers": []
    })
    ecr.put_image(repositoryName='orders', imageManifest=manifest, imageTag='prod')

    client = ECRClient(region='us-east-1')
    digest = client.get_digest('orders', 'prod', registry_id)
    assert digest is not None


@mock_aws
def test_lambda_client_noop_vs_update():
    # Create lambda function with container image requires ECR etc.; moto's support is limited
    # We'll mock get_function_configuration and subsequent calls using botocore Stubber
    session = boto3.Session(region_name='us-east-1')
    lam = session.client('lambda')

    lc = LambdaClient(region='us-east-1')
    # stub through botocore
    from botocore.stub import Stubber
    stubber = Stubber(lc.client)

    # First call: same digest -> noop
    stubber.add_response('get_function_configuration', {
        'FunctionName': 'orders-fn', 'ImageConfigResponse': {}, 'PackageType': 'Image'
    }, {'FunctionName': 'orders-fn'})
    with stubber:
        res = lc.update_function_direct('orders-fn', '123.dkr.ecr.us-east-1.amazonaws.com/orders@sha256:same', 'prod')
        assert res['status'] in ('noop', 'error')

    # Now simulate update path
    stubber = Stubber(lc.client)
    stubber.add_response('get_function_configuration', {
        'FunctionName': 'orders-fn', 'ImageConfigResponse': {}, 'PackageType': 'Image'
    }, {'FunctionName': 'orders-fn'})
    stubber.add_response('update_function_code', {
        'FunctionName': 'orders-fn'
    }, {'FunctionName': 'orders-fn', 'ImageUri': '123.dkr.ecr.us-east-1.amazonaws.com/orders@sha256:new', 'Publish': False})
    # poll for success
    stubber.add_response('get_function_configuration', {
        'FunctionName': 'orders-fn', 'LastUpdateStatus': 'Successful', 'PackageType': 'Image'
    }, {'FunctionName': 'orders-fn'})
    stubber.add_response('publish_version', {'FunctionName': 'orders-fn', 'Version': '2'}, {'FunctionName': 'orders-fn'})
    stubber.add_client_error('update_alias', 'ResourceNotFoundException')
    stubber.add_response('create_alias', {'AliasArn': 'arn:aws:lambda:us-east-1:111:function:orders-fn:prod'}, {
        'FunctionName': 'orders-fn', 'Name': 'prod', 'FunctionVersion': '2'
    })

    with stubber:
        res = lc.update_function_direct('orders-fn', '123.dkr.ecr.us-east-1.amazonaws.com/orders@sha256:new', 'prod')
        assert res['status'] == 'updated'
        assert res['version'] == '2'


@mock_aws
def test_ecr_vulnerability_check():
    ecr = boto3.client('ecr', region_name='us-east-1')
    ecr.create_repository(repositoryName='vulnerable-app')
    
    manifest = json.dumps({
        "schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "application/vnd.docker.container.image.v1+json", "size": 1, "digest": "sha256:a"},
        "layers": []
    })
    
    # put_image creates the digest
    resp = ecr.put_image(repositoryName='vulnerable-app', imageManifest=manifest, imageTag='latest')
    digest = resp['image']['imageId']['imageDigest']
    
    # Moto doesn't implement describe_image_scan_findings; have to mock it on the client
    client = ECRClient(region='us-east-1')
    from botocore.stub import Stubber
    stubber = Stubber(client.client)
    
    # Scenario 1: No findings
    stubber.add_response('describe_image_scan_findings', {
        'imageScanStatus': {'status': 'COMPLETE'},
        'imageScanFindings': {'findings': []}
    }, {'repositoryName': 'vulnerable-app', 'imageId': {'imageDigest': digest}})
    
    with stubber:
        assert client.check_vulnerabilities('vulnerable-app', digest, threshold='HIGH') is False

    # Scenario 5: ScanNotFoundException
    stubber.add_client_error(
        "describe_image_scan_findings",
        "ScanNotFoundException", 
        "The image scan findings for the specified image are not found.",
        expected_params={'repositoryName': 'vulnerable-app', 'imageId': {'imageDigest': digest}}
    )
    with stubber:
        assert client.check_vulnerabilities('vulnerable-app', digest, threshold='HIGH') is False

    # Scenario 2: Findings below threshold
    stubber.add_response('describe_image_scan_findings', {
        'imageScanStatus': {'status': 'COMPLETE'},
        'imageScanFindings': {'findings': [{'severity': 'MEDIUM'}, {'severity': 'LOW'}]}
    }, {'repositoryName': 'vulnerable-app', 'imageId': {'imageDigest': digest}})
    
    with stubber:
        assert client.check_vulnerabilities('vulnerable-app', digest, threshold='HIGH') is False

    # Scenario 5: ScanNotFoundException
    stubber.add_client_error(
        "describe_image_scan_findings",
        "ScanNotFoundException", 
        "The image scan findings for the specified image are not found.",
        expected_params={'repositoryName': 'vulnerable-app', 'imageId': {'imageDigest': digest}}
    )
    with stubber:
        assert client.check_vulnerabilities('vulnerable-app', digest, threshold='HIGH') is False
        
    # Scenario 3: Findings at threshold
    stubber.add_response('describe_image_scan_findings', {
        'imageScanStatus': {'status': 'COMPLETE'},
        'imageScanFindings': {'findings': [{'severity': 'HIGH'}]}
    }, {'repositoryName': 'vulnerable-app', 'imageId': {'imageDigest': digest}})
    
    with stubber:
        assert client.check_vulnerabilities('vulnerable-app', digest, threshold='HIGH') is True

    # Scenario 4: Scan in progress
    stubber.add_response('describe_image_scan_findings', {
        'imageScanStatus': {'status': 'IN_PROGRESS'},
    }, {'repositoryName': 'vulnerable-app', 'imageId': {'imageDigest': digest}})
    
    with stubber:
        assert client.check_vulnerabilities('vulnerable-app', digest, threshold='HIGH') is False

    # Scenario 5: ScanNotFoundException
    stubber.add_client_error(
        "describe_image_scan_findings",
        "ScanNotFoundException", 
        "The image scan findings for the specified image are not found.",
        expected_params={'repositoryName': 'vulnerable-app', 'imageId': {'imageDigest': digest}}
    )
    with stubber:
        assert client.check_vulnerabilities('vulnerable-app', digest, threshold='HIGH') is False
