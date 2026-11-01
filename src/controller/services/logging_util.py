import logging
import os

def setup_logging():
    level = os.environ.get("LOG_LEVEL", "INFO")
    # The Lambda Python runtime installs a root handler before our code runs, so
    # basicConfig() is a no-op there; set the level explicitly so INFO reaches logs.
    logging.basicConfig(level=getattr(logging, level))
    logging.getLogger().setLevel(getattr(logging, level))
    # boto3 logs "Found credentials in environment variables." per client at INFO.
    for noisy in ("botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
