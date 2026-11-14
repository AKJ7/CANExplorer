"""Network access of the QML engine (map tiles): an identifying User-Agent and a disk cache, as tile servers such as
OpenStreetMap's ask of applications (https://operations.osmfoundation.org/policies/tiles/)."""

import tempfile
from pathlib import Path

from PyQt6.QtCore import QStandardPaths
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkRequest
from PyQt6.QtQml import QQmlNetworkAccessManagerFactory

from can_explorer.__version__ import __version__

USER_AGENT = f'CANExplorer/{__version__} (+https://github.com/AKJ7/CANExplorer)'
CACHE_BYTES = 200 * 1024 * 1024


def cache_directory() -> Path:
    cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    return Path(cache or tempfile.gettempdir()) / 'tiles'


class _NetworkAccessManager(QNetworkAccessManager):
    def createRequest(self, operation, request, data=None):
        request = QNetworkRequest(request)
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, USER_AGENT)
        request.setAttribute(
            QNetworkRequest.Attribute.CacheLoadControlAttribute, QNetworkRequest.CacheLoadControl.PreferCache
        )
        return super().createRequest(operation, request, data)


class QmlNetworkFactory(QQmlNetworkAccessManagerFactory):
    """Creates the network access managers of the QML engine (one per loader thread)."""

    def create(self, parent):
        manager = _NetworkAccessManager(parent)
        cache = QNetworkDiskCache(manager)
        cache.setCacheDirectory(str(cache_directory()))
        cache.setMaximumCacheSize(CACHE_BYTES)
        manager.setCache(cache)
        return manager
