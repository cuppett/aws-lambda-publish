import boto3
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class MetricsClient:
    def __init__(self, namespace: str = "LambdaPublish", region: Optional[str] = None, credentials: Optional[Dict] = None):
        session = boto3.Session(
            region_name=region,
            aws_access_key_id=(credentials.get('AccessKeyId') if credentials else None),
            aws_secret_access_key=(credentials.get('SecretAccessKey') if credentials else None),
            aws_session_token=(credentials.get('SessionToken') if credentials else None)
        )
        self.client = session.client('cloudwatch')
        self.namespace = namespace

    def put_metric(self, metric_name: str, value: float = 1.0, unit: str = 'Count', dimensions: Optional[Dict[str, str]] = None):
        """Put a single metric to CloudWatch"""
        try:
            metric_data = {
                'MetricName': metric_name,
                'Value': value,
                'Unit': unit
            }
            
            if dimensions:
                metric_data['Dimensions'] = [{'Name': k, 'Value': v} for k, v in dimensions.items()]
            
            self.client.put_metric_data(
                Namespace=self.namespace,
                MetricData=[metric_data]
            )
            logger.debug(f"Published metric {metric_name}={value} to namespace {self.namespace}")
        except Exception as e:
            logger.warning(f"Failed to publish metric {metric_name}: {e}")

    def increment_counter(self, metric_name: str, dimensions: Optional[Dict[str, str]] = None):
        """Increment a counter metric by 1"""
        self.put_metric(metric_name, value=1.0, unit='Count', dimensions=dimensions)

    def record_updated_function(self, repository: str, tag: str, mode: str, status: str):
        """Record function update metrics with dimensions"""
        dimensions = {
            'Repository': repository,
            'Tag': tag,
            'Mode': mode,
            'Status': status
        }
        
        if status == 'updated':
            self.increment_counter('UpdatedFunctionCount', dimensions)
        elif status == 'noop-idempotent' or status == 'noop':
            self.increment_counter('NoOpCount', dimensions)
        else:
            self.increment_counter('Failures', dimensions)

    def record_pipeline_start(self, repository: str, tag: str, status: str):
        """Record pipeline start metrics"""
        dimensions = {
            'Repository': repository,
            'Tag': tag,
            'Type': 'pipeline'
        }
        
        if status == 'started':
            self.increment_counter('PipelineStartCount', dimensions)
        else:
            self.increment_counter('PipelineStartFailures', dimensions)

    def record_processing_time(self, duration_seconds: float, target_count: int):
        """Record overall processing time and target count"""
        self.put_metric('ProcessingDurationSeconds', value=duration_seconds, unit='Seconds')
        self.put_metric('TargetsProcessed', value=float(target_count), unit='Count')