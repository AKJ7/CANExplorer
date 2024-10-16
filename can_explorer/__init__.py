from decouple import config

LOG_FORMAT = config('CANEXPLORER_LOG_FORMAT', cast=str)
LOG_LEVEL = config('CANEXPLORER_LOG_LEVEL', cast=str)
PROJECT_NAME = config('CANEXPLORER_PROJECT_NAME', cast=str)
PROJECT_BUILD_DATE = '12 October 2024'
PROJECT_PLATFORM = 'Linux'

