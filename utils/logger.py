"""
utils/logger.py - Structured logging setup
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logger(name: str, log_file: str, level: str = "INFO") -> logging.Logger:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file - 10MB x 5 files
    fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# Module-level loggers
def get_logger(name: str) -> logging.Logger:
    from config import config
    return setup_logger(name, config.log_file, config.log_level)
