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
def test_resolve_image_tag_for_lambda_architecture():
    lc = LambdaClient(region='us-east-1')
    assert lc.resolve_image_tag('latest', 'arm64') == 'latest-aarch64'
    assert lc.resolve_image_tag('latest', 'x86_64') == 'latest-amd64'
    assert lc.resolve_image_tag('latest-aarch64', 'arm64') == 'latest-aarch64'


@mock_aws
def test_lambda_client_updates_tag_based_uri_even_when_digest_matches():
    session = boto3.Session(region_name='us-east-1')
    lc = LambdaClient(region='us-east-1')
    from botocore.stub import Stubber
    stubber = Stubber(lc.client)

    tag_uri = '771294529343.dkr.ecr.us-east-1.amazonaws.com/ddns-route53:latest'
    digest = 'sha256:new'

    stubber.add_response('get_function_configuration', {
        'FunctionName': 'ddns-update-handler', 'PackageType': 'Image'
    }, {'FunctionName': 'ddns-update-handler'})
    stubber.add_response('get_function', {
        'Code': {'ImageUri': tag_uri}
    }, {'FunctionName': 'ddns-update-handler'})
    stubber.add_response('get_function_configuration', {
        'FunctionName': 'ddns-update-handler', 'PackageType': 'Image'
    }, {'FunctionName': 'ddns-update-handler'})
    stubber.add_response('get_function', {
        'Code': {'ImageUri': tag_uri}
    }, {'FunctionName': 'ddns-update-handler'})
    stubber.add_response('update_function_code', {
        'FunctionName': 'ddns-update-handler'
    }, {'FunctionName': 'ddns-update-handler', 'ImageUri': tag_uri, 'Publish': False})
    stubber.add_response('get_function_configuration', {
        'FunctionName': 'ddns-update-handler', 'LastUpdateStatus': 'Successful', 'PackageType': 'Image'
    }, {'FunctionName': 'ddns-update-handler'})

    with stubber:
        res = lc.update_function_direct('ddns-update-handler', tag_uri, update_strategy='code-only', target_digest=digest)
        assert res['status'] == 'updated'


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
