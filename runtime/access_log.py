#!/usr/bin/env python3
"""Bound access logs to five 5 MiB files; lighttpd owns this pipe's lifecycle."""

import logging
from logging.handlers import RotatingFileHandler
import sys

handler = RotatingFileHandler("/var/cache/cgit/access.log", maxBytes=5 * 1024**2,
                              backupCount=4, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(message)s"))
logger = logging.getLogger("access")
logger.setLevel(logging.INFO)
logger.addHandler(handler)
for line in sys.stdin:
    logger.info(line.rstrip("\n"))
