# TODO: Fix parsing from .dev
import sys
from typing import Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=('.env', '.env.prod'),
        env_file_encoding='utf-8',
        cli_parse_args=True,
        extra='ignore',
        case_sensitive=False,
    )
    app_name: str = 'CANExplorer'
    debug_mode: Optional[bool] = True
    console_log_file: str = 'stdout'
    log_level: str = 'DEBUG'
    production_build: Optional[bool] = False
    log_format: str = '%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)-35s: %(message)s'
    date_format: str = '%Y-%m-%d %H:%M:%S'
    creation_year: int = 2025
    author: str = 'AKJ7'
    license_type: str = 'GPL3'
    platform: str = sys.platform
