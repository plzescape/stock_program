# -*- coding: utf-8 -*-
"""
qt_bootstrap.py — Qt 플랫폼 플러그인 경로 보정

[증상]
    qt.qpa.plugin: Could not find the Qt platform plugin "windows" in ""
    This application failed to start because no Qt platform plugin could
    be initialized.

[원인]
    프로젝트 경로에 한글이 들어 있으면(예: '부자가되고싶은 수지')
    Qt 가 내부적으로 계산하는 QLibraryInfo.PluginsPath 에서 비ASCII
    문자가 '?' 로 깨진다. 그래서 qwindows.dll 이 멀쩡히 있어도
    Qt 는 그 폴더를 찾지 못하고 죽는다.

    환경변수 QT_QPA_PLATFORM_PLUGIN_PATH 는 유니코드 그대로 전달되므로,
    PyQt5 패키지 위치에서 직접 계산해 넣어 주면 우회된다.

[사용]
    PyQt5 를 import 하기 전에 먼저 import 할 것.

        import qt_bootstrap  # noqa: F401
        from PyQt5.QtWidgets import QApplication

[근본 해결]
    프로젝트를 한글이 없는 경로(예: C:\\StockProgram)로 옮기면
    이 파일 없이도 동작한다. 한글 경로는 Qt 외에 다른 곳에서도
    문제를 일으킬 수 있으므로 그쪽이 더 안전하다.
"""

import os


def ensure_qt_plugin_path() -> bool:
    """플러그인 경로를 환경변수에 심는다. 실제로 설정했으면 True."""
    if os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH'):
        return False                      # 사용자가 이미 지정함 - 존중

    try:
        import PyQt5
    except ImportError:
        return False

    plugins = os.path.join(os.path.dirname(PyQt5.__file__), 'Qt5', 'plugins')
    platforms = os.path.join(plugins, 'platforms')
    if not os.path.isdir(platforms):
        return False                      # 설치 구조가 다름 - 건드리지 않음

    os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = platforms
    os.environ.setdefault('QT_PLUGIN_PATH', plugins)
    return True


ensure_qt_plugin_path()
