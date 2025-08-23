import boto3
import botocore
from typing import Any, Dict, List


def _from_ddb(av: Dict[str, Any]) -> Any:
    if 'S' in av:
        return av['S']
    if 'N' in av:
        n = av['N']
        try:
            if '.' in n:
                return float(n)
            return int(n)
        except Exception:
            return n
    if 'BOOL' in av:
        return av['BOOL']
    if 'M' in av:
        return {k: _from_ddb(v) for k, v in av['M'].items()}
    if 'L' in av:
        return [_from_ddb(v) for v in av['L']]
    if 'NULL' in av:
        return None
    return None


def _to_ddb(val: Any) -> Dict[str, Any]:
    if val is None:
        return {'NULL': True}
    if isinstance(val, bool):
        return {'BOOL': val}
    if isinstance(val, (int, float)):
        return {'N': str(val)}
    if isinstance(val, str):
        return {'S': val}
    if isinstance(val, list):
        return {'L': [_to_ddb(v) for v in val]}
    if isinstance(val, dict):
        return {'M': {k: _to_ddb(v) for k, v in val.items()}}
    return {'S': str(val)}


class DDBClient:
    def __init__(self, table_name, region=None, credentials=None):
        session = boto3.Session(region_name=region,
                                aws_access_key_id=(credentials.get('AccessKeyId') if credentials else None),
                                aws_secret_access_key=(credentials.get('SecretAccessKey') if credentials else None),
                                aws_session_token=(credentials.get('SessionToken') if credentials else None))
        self.client = session.client('dynamodb')
        self.table = table_name

    def get_targets(self, pk: str) -> List[Dict[str, Any]]:
        try:
            resp = self.client.query(
                TableName=self.table,
                KeyConditionExpression='PK = :pk',
                ExpressionAttributeValues={':pk': {'S': pk}}
            )
            items = resp.get('Items', [])
            out = []
            for it in items:
                out.append({k: _from_ddb(v) for k, v in it.items()})
            return out
        except botocore.exceptions.BotoCoreError:
            return []
        except Exception:
            return []

    def update_last_processed(self, pk: str, sk: str, digest: str, status: str) -> bool:
        try:
            self.client.update_item(
                TableName=self.table,
                Key={'PK': {'S': pk}, 'SK': {'S': sk}},
                UpdateExpression='SET lastProcessedDigest = :d, lastStatus = :s',
                ExpressionAttributeValues={':d': {'S': digest}, ':s': {'S': status}}
            )
            return True
        except Exception:
            return False

    def conditional_set_processed(self, pk: str, sk: str, new_digest: str) -> bool:
        try:
            self.client.update_item(
                TableName=self.table,
                Key={'PK': {'S': pk}, 'SK': {'S': sk}},
                UpdateExpression='SET lastProcessedDigest = :d',
                ConditionExpression='attribute_not_exists(lastProcessedDigest) OR lastProcessedDigest <> :d',
                ExpressionAttributeValues={':d': {'S': new_digest}}
            )
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            return False
        except Exception:
            return False

    def record_pipeline_execution(self, pk: str, sk: str, execution_id: str, status: str = 'Started') -> bool:
        try:
            self.client.update_item(
                TableName=self.table,
                Key={'PK': {'S': pk}, 'SK': {'S': sk}},
                UpdateExpression='SET lastExecutionId = :e, lastStatus = :s',
                ExpressionAttributeValues={':e': {'S': execution_id}, ':s': {'S': status}}
            )
            return True
        except Exception:
            return False
