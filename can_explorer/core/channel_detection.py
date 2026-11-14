"""Channel detection: ask every python-can backend which channels it can connect to, and record why it found none.

`can.detect_available_configs` returns one flat list: a backend whose driver library cannot be loaded, one that
raises while enumerating its devices and one that simply has no device all look the same (nothing found), so a
missing device cannot be diagnosed. This module probes each backend on its own and reports its outcome:

- found / none: detection ran and found channels, or none;
- unavailable: the backend could not be loaded (typically its vendor driver library is not installed);
- unsupported: the backend cannot detect channels (they have to be typed in);
- error: detection raised (the error is kept);
- timeout: detection did not finish in time.

Backends are probed in parallel, since some driver libraries enumerate their devices slowly. Some vendor libraries
may not tolerate being loaded or enumerated while other drivers are, so the backends that failed (unavailable or
error) are probed again, one at a time; a backend that then works is marked as found on its own.
"""

import concurrent.futures
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List

import can
from can.exceptions import CanInterfaceNotImplementedError
from can.interface import _get_class_for_interface  # python-can's own backend loader (not public API)

FOUND = 'found'
NONE = 'none'
UNAVAILABLE = 'unavailable'
UNSUPPORTED = 'unsupported'
ERROR = 'error'
TIMEOUT = 'timeout'
FAILED = (UNAVAILABLE, ERROR)  # Retried alone
DETECTION_TIMEOUT = 15.0


@dataclass(frozen=True)
class BackendProbe:
    interface: str
    status: str
    configs: List[Dict[str, Any]] = field(default_factory=list)
    detail: str = ''  # Why nothing was found: the load error, the exception, ...
    seconds: float = 0.0
    alone: bool = False  # Probed again on its own after failing alongside the other backends

    def summary(self) -> str:
        """One line for logs and the diagnostics report, e.g. 'etas: error after 0.31 s: OSError(...)'."""
        timing = f'{self.seconds:.2f} s'
        if self.status == FOUND:
            channels = ', '.join(str(config.get('channel')) for config in self.configs)
            text = f'{len(self.configs)} channel(s) in {timing}: {channels}'
        elif self.status == NONE:
            text = f'no channels ({timing})'
        elif self.status == TIMEOUT:
            text = f'timed out after {timing}'
        else:
            text = f'{self.status} after {timing}' + (f': {self.detail}' if self.detail else '')
        return f'{self.interface}: {text}' + (' [probed alone]' if self.alone else '')


def probe_backend(interface: str) -> BackendProbe:
    """Load one backend and detect its channels, without raising."""
    start = time.perf_counter()

    def result(status: str, configs=(), detail: str = '') -> BackendProbe:
        return BackendProbe(interface, status, list(configs), detail, time.perf_counter() - start)

    try:
        bus_class = _get_class_for_interface(interface)
    except CanInterfaceNotImplementedError as e:
        return result(UNAVAILABLE, detail=str(e))
    except NotImplementedError as e:
        return result(UNAVAILABLE, detail=str(e))
    except Exception as e:  # A backend's import may raise anything (e.g. OSError loading its DLL)
        return result(UNAVAILABLE, detail=repr(e))
    try:
        configs = list(bus_class._detect_available_configs())  # pylint: disable=protected-access
    except NotImplementedError:
        return result(UNSUPPORTED, detail='does not support channel detection')
    except Exception as e:
        return result(ERROR, detail=repr(e))
    for config in configs:
        config.setdefault('interface', interface)
    return result(FOUND if configs else NONE, configs)


def detect_channels(
    interfaces: Iterable[str] = None,
    timeout: float = DETECTION_TIMEOUT,
    probe: Callable[[str], BackendProbe] = probe_backend,
    retry_alone: bool = True,
) -> List[BackendProbe]:
    """Probe the backends (by default all python-can knows) and return one result per backend, sorted by name.

    All backends are probed in parallel within `timeout` seconds; then each one that failed is probed again on its
    own, the retries sharing another `timeout` seconds. A backend still running at a deadline is reported as timed
    out (its thread cannot be stopped and is left to finish in the background).
    """
    names = sorted(can.interfaces.VALID_INTERFACES if interfaces is None else set(interfaces))
    # A thread per backend (plus one for the retries): with fewer, probes would queue and could time out unstarted
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(names) + 1, thread_name_prefix='channel-detection')
    try:
        futures = {executor.submit(probe, name): name for name in names}
        concurrent.futures.wait(futures, timeout=timeout)
        results = {futures[future]: _outcome(future, futures[future], timeout) for future in futures}
        if retry_alone:
            deadline = time.monotonic() + timeout
            for name in [name for name in names if results[name].status in FAILED]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                future = executor.submit(probe, name)
                # wait() does not raise: a probe that raises is reported as an error by _outcome
                concurrent.futures.wait([future], timeout=remaining)
                retried = _outcome(future, name, remaining)
                first = results[name]
                detail = retried.detail
                if retried.status not in FAILED and first.detail:
                    detail = f'failed alongside the other backends: {first.detail}'
                results[name] = replace(retried, detail=detail, alone=True)
                if (
                    retried.status == TIMEOUT
                ):  # It took the time left; the clock may not show it yet (coarse on Windows)
                    break
        return [results[name] for name in names]
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _outcome(future: concurrent.futures.Future, interface: str, timeout: float) -> BackendProbe:
    if not future.done():
        return BackendProbe(interface, TIMEOUT, seconds=timeout)
    try:
        return future.result()
    except Exception as e:  # probe_backend does not raise; a replacement probe might
        return BackendProbe(interface, ERROR, detail=repr(e))


def configs_of(probes: Iterable[BackendProbe]) -> List[Dict[str, Any]]:
    """The detected configurations of all backends, as can.detect_available_configs returns them."""
    return [config for probe in probes for config in probe.configs]
