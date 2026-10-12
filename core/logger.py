"""
ZenSearch - 统一日志系统 (Logging & Diagnostics)
提供控制台分级输出及本地文件记录 (ZenSearch.log)。
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler

LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ZenSearch.log")

def setup_logger(debug_mode: bool = False) -> logging.Logger:
    logger = logging.getLogger("ZenSearch")
    logger.setLevel(logging.DEBUG if debug_mode else logging.INFO)

    # 避免重复 handler
    if logger.handlers:
        return logger

    # 1. 滚动文件记录 (最大 5MB, 保留 3 个备份)
    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_fmt = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] [%(filename)s:%(lineno)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler.setFormatter(file_fmt)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"[Warning] Failed to initialize file logger: {e}", file=sys.stderr)

    # 2. 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)
    console_handler.setLevel(logging.DEBUG if debug_mode else logging.INFO)
    logger.addHandler(console_handler)

    return logger

# 默认全局实例
logger = setup_logger(debug_mode=True)
