from can_explorer.sys.environment import sanitize_snap_environment

SNAP_ENVIRONMENT = {
    'SNAP': '/snap/code/271',
    'SNAP_USER_DATA': '/home/user/snap/code/271',
    'SNAP_USER_COMMON': '/home/user/snap/code/common',
    'GSETTINGS_SCHEMA_DIR': '/home/user/snap/code/271/.local/share/glib-2.0/schemas',
    'GTK_PATH': '/snap/code/271/usr/lib/x86_64-linux-gnu/gtk-3.0',
    'GIO_MODULE_DIR': '/home/user/snap/code/common/.cache/gio-modules',
    'LOCPATH': '/snap/code/271/usr/lib/locale',
    'XDG_DATA_DIRS': '/home/user/snap/code/271/.local/share:/snap/code/271/usr/share:/usr/share',
    'XDG_DATA_DIRS_VSCODE_SNAP_ORIG': '/usr/share/ubuntu:/usr/share',
    'GDK_BACKEND_VSCODE_SNAP_ORIG': '',
    'GDK_BACKEND': 'x11',
    'GTK_MODULES': 'gail:atk-bridge',
    'HOME': '/home/user',
}


def test_restores_environment_leaked_by_snap():
    environ = dict(SNAP_ENVIRONMENT)
    changed = sanitize_snap_environment(environ, executable='/home/user/project/.venv/bin/python')
    assert environ['XDG_DATA_DIRS'] == '/usr/share/ubuntu:/usr/share'
    assert 'GDK_BACKEND' not in environ
    for name in ('GSETTINGS_SCHEMA_DIR', 'GTK_PATH', 'GIO_MODULE_DIR', 'LOCPATH'):
        assert name not in environ
    assert not any(key.endswith('_VSCODE_SNAP_ORIG') for key in environ)
    assert environ['GTK_MODULES'] == 'gail:atk-bridge' and environ['HOME'] == '/home/user'
    assert changed == ['GDK_BACKEND', 'GIO_MODULE_DIR', 'GSETTINGS_SCHEMA_DIR', 'GTK_PATH', 'LOCPATH', 'XDG_DATA_DIRS']


def test_strips_snap_entries_without_saved_original():
    environ = {k: v for k, v in SNAP_ENVIRONMENT.items() if not k.endswith('_VSCODE_SNAP_ORIG')}
    sanitize_snap_environment(environ, executable='/usr/bin/python3')
    assert environ['XDG_DATA_DIRS'] == '/usr/share'


def test_untouched_outside_of_snaps_and_inside_the_snap():
    environ = {'GTK_PATH': '/usr/lib/gtk-3.0', 'XDG_DATA_DIRS': '/usr/share'}
    assert sanitize_snap_environment(environ, executable='/usr/bin/python3') == []
    assert environ == {'GTK_PATH': '/usr/lib/gtk-3.0', 'XDG_DATA_DIRS': '/usr/share'}
    environ = dict(SNAP_ENVIRONMENT)
    assert sanitize_snap_environment(environ, executable='/snap/code/271/usr/bin/python3') == []
    assert environ == SNAP_ENVIRONMENT
