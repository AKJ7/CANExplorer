import sys
import logging
from can_explorer import settings


class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m',  # Cyan
        'INFO': '\033[32m',  # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',  # Red
        'CRITICAL': '\033[35m',  # Magenta
    }
    RESET = '\033[0m'

    def format(self, record: logging.LogRecord) -> str:
        if not settings.production_build:
            color = self.COLORS.get(record.levelname, '')
            record.levelname = f'{color}{record.levelname}{self.RESET}'
        return super().format(record)


def setup_logging() -> None:
    log_level: str = getattr(logging, settings.log_level)
    logging.root.setLevel(log_level)
    console_log_destination = getattr(sys, settings.console_log_file)
    console_handler = logging.StreamHandler(console_log_destination)
    console_handler.setLevel(log_level)
    if settings.production_build:
        formatter = logging.Formatter(fmt=settings.log_format, datefmt=settings.date_format)
    else:
        formatter = ColoredFormatter(fmt=settings.log_format, datefmt=settings.date_format)
    console_handler.setFormatter(formatter)
    logging.root.addHandler(console_handler)


def get_logger(file_name: str) -> logging.Logger:
    if settings.production_build:
        return logging.getLogger(name=f'{settings.app_name}.{file_name}')
    else:
        return logging.getLogger(file_name)
