import logging
import os

def setup_logger(name: str, filename: str) -> logging.Logger:
    os.makedirs("logs", exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # 중복 핸들러 방지 (재실행/재import 시)
    if logger.handlers:
        return logger

    handler = logging.FileHandler(os.path.join("logs", filename), encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
