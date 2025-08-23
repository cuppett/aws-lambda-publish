import os
import json
import logging
import boto3
import time
from typing import Dict, Any, List
from src.controller.services.ddb_client import DDBClient
from src.controller.services.sts_client import STSClient
from src.controller.services.metrics_client import MetricsClient

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))


def handler(event, context):
    table = os.environ.get('TABLE_NAME', 'ImageTagSubscriptions')
    region = os.environ.get('AWS_REGION')
    metrics_namespace = os.environ.get('METRICS_NAMESPACE', 'LambdaPublish')
    
    ddb = DDBClient(table_name=table, region=region)
    metrics = MetricsClient(namespace=metrics_namespace, region=region)
    
    logger.info("Starting monitor execution")
    
    try:
        # Scan for items with pending pipeline executions
        pending_items = scan_pending_executions(ddb)
        
        if not pending_items:
            logger.info("No pending executions found")
            return {"status": "ok", "pending_count": 0}
        
        logger.info(f"Found {len(pending_items)} pending executions to check")
        
        processed_count = 0
        for item in pending_items:
            try:
                if process_pending_execution(item, ddb, metrics, region):
                    processed_count += 1
            except Exception as e:
                logger.exception(f"Failed to process item {item.get('PK', 'unknown')}")
                metrics.increment_counter("MonitorProcessingErrors")
        
        metrics.put_metric("PendingExecutionsChecked", float(len(pending_items)))
        metrics.put_metric("ExecutionsUpdated", float(processed_count))
        
        logger.info(f"Monitor execution completed. Checked: {len(pending_items)}, Updated: {processed_count}")
        
        return {
            "status": "ok", 
            "pending_count": len(pending_items),
            "processed_count": processed_count
        }
    
    except Exception as e:
        logger.exception("Monitor execution failed")
        metrics.increment_counter("MonitorErrors")
        return {"status": "error", "error": str(e)}


def scan_pending_executions(ddb: DDBClient) -> List[Dict[str, Any]]:
    """Scan DynamoDB for items with lastExecutionId and non-final status"""
    try:
        # In a real implementation, you'd want to use pagination for large tables
        # This is a simplified version that scans all items
        response = ddb.client.scan(
            TableName=ddb.table,
            FilterExpression='attribute_exists(lastExecutionId) AND (attribute_not_exists(lastStatus) OR lastStatus IN (:pending, :running, :started))',
            ExpressionAttributeValues={
                ':pending': {'S': 'Pending'},
                ':running': {'S': 'Running'},
                ':started': {'S': 'Started'}
            }
        )
        
        items = []
        for raw_item in response.get('Items', []):
            # Convert from DDB format to regular dict
            from src.controller.services.ddb_client import _from_ddb
            item = {k: _from_ddb(v) for k, v in raw_item.items()}
            items.append(item)
        
        return items
    except Exception as e:
        logger.exception("Failed to scan for pending executions")
        return []


def process_pending_execution(item: Dict[str, Any], ddb: DDBClient, metrics: MetricsClient, region: str) -> bool:
    """Process a single pending execution item"""
    pk = item.get('PK')
    sk = item.get('SK')
    execution_id = item.get('lastExecutionId')
    mode = item.get('mode', 'unknown')
    
    if not execution_id or not pk or not sk:
        logger.warning(f"Missing required fields in item: {item}")
        return False
    
    try:
        # Determine if this is a pipeline or deployment
        if mode == 'pipeline':
            status = check_pipeline_status(item, execution_id, region)
        else:
            # For direct mode, we don't typically have executions to monitor
            logger.debug(f"Skipping direct mode item: {pk}")
            return False
        
        if status and status != item.get('lastStatus'):
            # Update the status in DynamoDB
            success = ddb.update_last_processed(
                pk, sk, 
                item.get('lastProcessedDigest', ''), 
                status
            )
            
            if success:
                logger.info(f"Updated status for {pk}/{sk}: {status}")
                
                # Record metrics
                if status in ('Succeeded', 'Failed', 'Stopped'):
                    metrics.increment_counter("ExecutionStatusUpdates", {
                        "Status": status,
                        "Mode": mode
                    })
                
                return True
            else:
                logger.error(f"Failed to update status for {pk}/{sk}")
                return False
    
    except Exception as e:
        logger.exception(f"Failed to process execution {execution_id}")
        return False
    
    return False


def check_pipeline_status(item: Dict[str, Any], execution_id: str, region: str) -> str:
    """Check the status of a CodePipeline execution"""
    target = item.get('target', {})
    target_region = target.get('region', region)
    assume_role = item.get('pipelineAssumeRoleArn') or item.get('assumeRoleArn')
    
    try:
        # Get credentials if cross-account
        creds = None
        if assume_role:
            sts = STSClient()
            creds = sts.assume_role(assume_role)
        
        # Create CodePipeline client
        session = boto3.Session(
            region_name=target_region,
            aws_access_key_id=(creds.get('AccessKeyId') if creds else None),
            aws_secret_access_key=(creds.get('SecretAccessKey') if creds else None),
            aws_session_token=(creds.get('SessionToken') if creds else None)
        )
        pipeline_client = session.client('codepipeline')
        
        # Get pipeline execution details
        pipeline_name = item.get('pipeline', {}).get('name')
        if not pipeline_name:
            logger.error(f"Missing pipeline name for execution {execution_id}")
            return None
        
        response = pipeline_client.get_pipeline_execution(
            pipelineName=pipeline_name,
            pipelineExecutionId=execution_id
        )
        
        execution = response.get('pipelineExecution', {})
        status = execution.get('status')
        
        logger.debug(f"Pipeline {pipeline_name} execution {execution_id} status: {status}")
        
        # Map CodePipeline statuses to our internal statuses
        status_mapping = {
            'InProgress': 'Running',
            'Stopped': 'Stopped',
            'Stopping': 'Stopping',
            'Succeeded': 'Succeeded',
            'Superseded': 'Superseded',
            'Failed': 'Failed'
        }
        
        return status_mapping.get(status, status)
    
    except pipeline_client.exceptions.PipelineExecutionNotFoundException:
        logger.warning(f"Pipeline execution {execution_id} not found")
        return 'NotFound'
    except Exception as e:
        logger.exception(f"Failed to check pipeline status for {execution_id}")
        return None
