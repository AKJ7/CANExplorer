import sys
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix='CANEXPLORER_',
        env_file=('.env', '.env.prod'),
        env_file_encoding='utf-8',
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

    def apply_cli_args(self, args: Optional[list[str]] = None) -> None:
        """Override the settings in place from command line arguments.

        Kept out of __init__ so that importing the package (e.g. from pytest) does not parse foreign arguments.
        """
        parsed = type(self)(_cli_parse_args=sys.argv[1:] if args is None else args)
        for name in type(self).model_fields:
            setattr(self, name, getattr(parsed, name))
