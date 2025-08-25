import os

class Config:
    def __init__(self):
        self.table_name = os.environ.get("TABLE_NAME", "ImageTagSubscriptions")
        self.max_parallel_targets = int(os.environ.get("MAX_PARALLEL_TARGETS", "10"))
        self.assume_role_session_name = os.environ.get("ASSUME_ROLE_SESSION_NAME", "lambda-publish")
        self.default_mode = os.environ.get("DEFAULT_MODE", "direct")
        self.default_update_strategy = os.environ.get("DEFAULT_UPDATE_STRATEGY", "publish-and-alias")
        self.metrics_namespace = os.environ.get("METRICS_NAMESPACE", "LambdaPublish")
        self.log_level = os.environ.get("LOG_LEVEL", "INFO")
        self.scan_severity_threshold = os.environ.get("SCAN_SEVERITY_THRESHOLD", "HIGH")
