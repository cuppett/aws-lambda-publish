import os
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.controller.services.ecr_client import ECRClient
from src.controller.services.ddb_client import DDBClient
from src.controller.services.sts_client import STSClient
from src.controller.services.lambda_client import LambdaClient
from src.controller.services.pipeline_client import PipelineClient
from src.controller.services.logging_util import setup_logging
from src.controller.services.config import Config

setup_logging()
logger = logging.getLogger("controller")

config = Config()


def handler(event, context):
    correlation_id = event.get("id") or str(uuid.uuid4())
    logger.info(json.dumps({"msg": "received", "correlationId": correlation_id, "event": event}))
    detail = event.get("detail", {})
    repository = detail.get("repository-name")
    image_tag = None
    if "image-tag" in detail:
        image_tag = detail.get("image-tag")
    elif "image-tags" in detail:
        tags = detail.get("image-tags")
        if isinstance(tags, list) and tags:
            image_tag = tags[0]
    if not repository or not image_tag:
        logger.error(json.dumps({"msg": "missing repository or image tag", "correlationId": correlation_id}))
        return {"status": "ignored"}

    registry_id = detail.get("registry-id")
    region = os.environ.get("AWS_REGION")

    ecr = ECRClient(region=region)
    digest = ecr.get_digest(repository, image_tag, registry_id)
    if not digest:
        logger.error(json.dumps({"msg": "no digest", "correlationId": correlation_id}))
        return {"status": "error", "reason": "no_digest"}

    ddb = DDBClient(table_name=config.table_name)
    pk = f"REG#{registry_id}#REPO#{repository}#TAG#{image_tag}"
    targets = ddb.get_targets(pk)
    if not targets:
        logger.info(json.dumps({"msg": "no targets", "pk": pk, "correlationId": correlation_id}))
        return {"status": "no_targets"}

    max_workers = config.max_parallel_targets
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {}
        for t in targets:
            futures[ex.submit(process_target, t, repository, digest, registry_id, region, pk, correlation_id)] = t
        for fut in as_completed(futures):
            try:
                res = fut.result()
                results.append(res)
            except Exception:
                logger.exception("target processing failed")
    return {"status": "done", "results": results}


def process_target(target_item, repository, digest, registry_id, hub_region, pk, correlation_id):
    mode = target_item.get("mode") or config.default_mode
    target = target_item.get("target", {})
    region = target.get("region", hub_region)
    function_name = target.get("functionName")
    alias = target.get("aliasName")
    assume_role = target_item.get("assumeRoleArn") or target.get("assumeRoleArn")
    sk = target_item.get("SK") or f"TARGET#{region}#{target.get('accountId', '')}#{function_name}"

    creds = None
    if assume_role:
        sts = STSClient()
        creds = sts.assume_role(assume_role)

    image_uri = f"{registry_id}.dkr.ecr.{region}.amazonaws.com/{repository}@{digest}"

    if mode == "direct":
        lc = LambdaClient(region=region, credentials=creds)
        new_digest = image_uri.split('@', 1)[1]
        if not config.default_update_strategy:
            pass
        ok = ddb_conditional_set(pk, sk, new_digest)
        if not ok:
            return {"function": function_name, "status": "noop-idempotent"}
        res = with_retries(lambda: lc.update_function_direct(function_name, image_uri, alias))
        status = res.get("status")
        DDBClient(table_name=config.table_name).update_last_processed(pk, sk, new_digest, status)
        return res
    else:
        pc = PipelineClient(region=region, credentials=creds)
        pipeline = target_item.get("pipeline") or target.get("pipeline") or {}
        pipeline_name = pipeline.get("name")
        vars = {
            "IMAGE_URI": image_uri,
            "FUNCTION_NAME": function_name or "",
            "ALIAS_NAME": alias or "",
            "DEPLOY_APP": (target_item.get("codeDeploy") or target.get("codeDeploy") or {}).get("applicationName", ""),
            "DEPLOY_GROUP": (target_item.get("codeDeploy") or target.get("codeDeploy") or {}).get("deploymentGroupName", ""),
            "DEPLOY_CONFIG": (target_item.get("codeDeploy") or target.get("codeDeploy") or {}).get("deploymentConfigName", "")
        }
        res = with_retries(lambda: pc.start_pipeline(pipeline_name, vars))
        execution_id = res.get("executionId")
        DDBClient(table_name=config.table_name).record_pipeline_execution(pk, sk, execution_id or "")
        return res


def with_retries(fn, retries=3, base_delay=0.5):
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(base_delay * (2 ** i))


def ddb_conditional_set(pk, sk, digest):
    return DDBClient(table_name=config.table_name).conditional_set_processed(pk, sk, digest)
