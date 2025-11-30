import asyncio
from can_explorer.sys.version import get_version

__version__ = get_version(asyncio.get_event_loop())
