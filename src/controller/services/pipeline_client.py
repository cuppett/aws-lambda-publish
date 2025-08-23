import boto3
import json
import time
import uuid
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class PipelineClient:
    def __init__(self, region=None, credentials=None):
        session = boto3.Session(region_name=region,
                                aws_access_key_id=(credentials.get('AccessKeyId') if credentials else None),
                                aws_secret_access_key=(credentials.get('SecretAccessKey') if credentials else None),
                                aws_session_token=(credentials.get('SessionToken') if credentials else None))
        self.client = session.client('codepipeline')
        self.ssm_client = session.client('ssm')

    def start_pipeline(self, name: str, variables: Dict[str, str], variable_strategy: str = "parameter_store"):
        """
        Start pipeline with variable propagation strategies:
        - parameter_store: Store variables in SSM Parameter Store with pipeline execution prefix
        - client_token: Pass IMAGE_URI as clientRequestToken (limited to 128 chars)
        - environment: Store variables as pipeline execution environment (requires pipeline support)
        """
        try:
            if not name:
                return {"pipeline": name, "status": "error", "error": "Pipeline name is required"}
            
            execution_id = str(uuid.uuid4())
            
            # Prepare pipeline execution parameters
            kwargs = {"name": name}
            
            # Apply variable propagation strategy
            if variable_strategy == "parameter_store":
                # Store variables in Parameter Store with execution prefix
                param_prefix = f"/lambda-publish/pipeline/{name}/{execution_id}"
                success = self._store_variables_in_ssm(param_prefix, variables)
                if not success:
                    logger.warning(f"Failed to store some variables in Parameter Store for {name}")
                
                # Use execution ID as client request token for correlation
                kwargs["clientRequestToken"] = execution_id
                
            elif variable_strategy == "client_token":
                # Legacy approach: pass IMAGE_URI as client request token
                token = variables.get('IMAGE_URI', '')
                if token:
                    kwargs["clientRequestToken"] = token[:128]  # API limit
                    
            else:
                logger.warning(f"Unknown variable strategy: {variable_strategy}")
                kwargs["clientRequestToken"] = execution_id
            
            logger.info(f"Starting pipeline {name} with strategy {variable_strategy}")
            logger.debug(f"Pipeline variables: {variables}")
            
            # Start pipeline execution
            resp = self.client.start_pipeline_execution(**kwargs)
            actual_execution_id = resp.get('pipelineExecutionId')
            
            # If using parameter store, update the parameters with the actual execution ID
            if variable_strategy == "parameter_store" and actual_execution_id != execution_id:
                old_prefix = f"/lambda-publish/pipeline/{name}/{execution_id}"
                new_prefix = f"/lambda-publish/pipeline/{name}/{actual_execution_id}"
                self._move_ssm_parameters(old_prefix, new_prefix, variables)
            
            logger.info(f"Pipeline {name} started successfully. Execution ID: {actual_execution_id}")
            
            return {
                "pipeline": name, 
                "status": "started", 
                "executionId": actual_execution_id,
                "variableStrategy": variable_strategy,
                "variableCount": len(variables)
            }
            
        except self.client.exceptions.PipelineNotFoundException:
            logger.error(f"Pipeline {name} not found")
            return {"pipeline": name, "status": "error", "error": "Pipeline not found"}
        except self.client.exceptions.InvalidStructureException as e:
            logger.error(f"Invalid pipeline structure for {name}: {e}")
            return {"pipeline": name, "status": "error", "error": f"Invalid pipeline structure: {str(e)}"}
        except Exception as e:
            logger.exception(f"Failed to start pipeline {name}")
            return {"pipeline": name, "status": "error", "error": str(e)}

    def _store_variables_in_ssm(self, prefix: str, variables: Dict[str, str]) -> bool:
        """Store variables in SSM Parameter Store with the given prefix"""
        try:
            for key, value in variables.items():
                if not value:  # Skip empty values
                    continue
                    
                param_name = f"{prefix}/{key}"
                
                try:
                    self.ssm_client.put_parameter(
                        Name=param_name,
                        Value=value,
                        Type='String',
                        Overwrite=True,
                        Description=f"Pipeline variable for {key}",
                        # Set TTL-like behavior with tags
                        Tags=[
                            {'Key': 'Source', 'Value': 'lambda-publish'},
                            {'Key': 'Type', 'Value': 'pipeline-variable'},
                            {'Key': 'CreatedAt', 'Value': str(int(time.time()))}
                        ]
                    )
                    logger.debug(f"Stored parameter {param_name}")
                except Exception as e:
                    logger.warning(f"Failed to store parameter {param_name}: {e}")
                    return False
            
            return True
        except Exception as e:
            logger.exception(f"Failed to store variables in SSM with prefix {prefix}")
            return False

    def _move_ssm_parameters(self, old_prefix: str, new_prefix: str, variables: Dict[str, str]):
        """Move SSM parameters from old prefix to new prefix (cleanup after execution ID change)"""
        try:
            for key in variables.keys():
                old_param = f"{old_prefix}/{key}"
                new_param = f"{new_prefix}/{key}"
                
                try:
                    # Get the parameter value
                    response = self.ssm_client.get_parameter(Name=old_param)
                    value = response['Parameter']['Value']
                    
                    # Create parameter with new name
                    self.ssm_client.put_parameter(
                        Name=new_param,
                        Value=value,
                        Type='String',
                        Overwrite=True,
                        Description=f"Pipeline variable for {key}",
                        Tags=[
                            {'Key': 'Source', 'Value': 'lambda-publish'},
                            {'Key': 'Type', 'Value': 'pipeline-variable'}
                        ]
                    )
                    
                    # Delete old parameter
                    self.ssm_client.delete_parameter(Name=old_param)
                    
                except Exception as e:
                    logger.warning(f"Failed to move parameter from {old_param} to {new_param}: {e}")
                    
        except Exception as e:
            logger.exception(f"Failed to move parameters from {old_prefix} to {new_prefix}")

    def get_pipeline_variables(self, pipeline_name: str, execution_id: str) -> Dict[str, str]:
        """Retrieve pipeline variables from Parameter Store for a given execution"""
        try:
            prefix = f"/lambda-publish/pipeline/{pipeline_name}/{execution_id}"
            
            response = self.ssm_client.get_parameters_by_path(
                Path=prefix,
                Recursive=True
            )
            
            variables = {}
            for param in response.get('Parameters', []):
                # Extract variable name from parameter name
                var_name = param['Name'].split('/')[-1]
                variables[var_name] = param['Value']
            
            logger.debug(f"Retrieved {len(variables)} variables for pipeline {pipeline_name} execution {execution_id}")
            return variables
            
        except Exception as e:
            logger.exception(f"Failed to get pipeline variables for {pipeline_name}/{execution_id}")
            return {}
