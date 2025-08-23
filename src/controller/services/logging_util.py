import logging
import os

def setup_logging():
    level = os.environ.get("LOG_LEVEL", "INFO")
    logging.basicConfig(level=getattr(logging, level))
