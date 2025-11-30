import asyncio
from can_explorer.util.version import SemanticVersion
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)


async def get_project_git_version() -> SemanticVersion:
    cmd = 'git describe --dirty --tags'
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    error_msg = stderr.decode().strip()
    out_msg = stdout.decode().strip()
    if error_msg != '':
        logger.info(f'Could not read version. Got: {error_msg}')
        version_str = ''
    else:
        version_str = out_msg
    version = SemanticVersion.from_str(version_str)
    return version


def get_version(loop: asyncio.AbstractEventLoop):
    version = loop.run_until_complete(get_project_git_version())
    return version
