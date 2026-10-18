import os
import sys
from typing import List, MutableMapping

# Variables a snap (e.g. the VS Code snap) points at its own bundled GTK/GLib runtime
SNAP_RUNTIME_VARIABLES = (
    'GDK_PIXBUF_MODULEDIR',
    'GDK_PIXBUF_MODULE_FILE',
    'GIO_MODULE_DIR',
    'GSETTINGS_SCHEMA_DIR',
    'GTK_EXE_PREFIX',
    'GTK_IM_MODULE_FILE',
    'GTK_PATH',
    'LOCPATH',
)
SNAP_PATH_LIST_VARIABLES = ('XDG_DATA_DIRS', 'XDG_CONFIG_DIRS')
XDG_SEPARATOR = ':'  # Defined by the XDG Base Directory specification, also where os.pathsep differs
VSCODE_SNAP_SUFFIX = '_VSCODE_SNAP_ORIG'


def sanitize_snap_environment(
    environ: MutableMapping[str, str] = os.environ, executable: str = sys.executable
) -> List[str]:
    """Undo the environment a snap leaks into processes started from it.

    Started from the terminal of a snap application (typically VS Code installed as a snap), the
    system GTK used by Qt would load the snap's GSettings schemas and GIO/GTK modules. These do not
    match the system libraries and GLib aborts, e.g. with "Settings schema
    'org.gnome.settings-daemon.plugins.xsettings' does not contain a key named 'antialiasing'".

    Must run before the QApplication is created. Returns the names of the variables that were changed.
    """
    snap_dirs = [environ[key] for key in ('SNAP', 'SNAP_USER_DATA', 'SNAP_USER_COMMON') if environ.get(key)]
    if not snap_dirs or executable.startswith(environ.get('SNAP', '\0')):
        return []  # Not started from a snap, or running inside the snap itself

    def in_snap(path: str) -> bool:
        return any(path == snap_dir or path.startswith(snap_dir.rstrip('/') + '/') for snap_dir in snap_dirs)

    changed = []
    # VS Code keeps the values from before the snap launcher modified them
    for key in [key for key in environ if key.endswith(VSCODE_SNAP_SUFFIX)]:
        name = key.removesuffix(VSCODE_SNAP_SUFFIX)
        original = environ.pop(key)
        if original:
            environ[name] = original
        else:
            environ.pop(name, None)
        changed.append(name)
    for name in SNAP_RUNTIME_VARIABLES:
        if in_snap(environ.get(name, '')):
            del environ[name]
            changed.append(name)
    for name in SNAP_PATH_LIST_VARIABLES:
        if name not in environ:
            continue
        entries = environ[name].split(XDG_SEPARATOR)
        kept = [entry for entry in entries if not in_snap(entry)]
        if kept != entries:
            environ[name] = XDG_SEPARATOR.join(kept)
            changed.append(name)
    return sorted(set(changed))
