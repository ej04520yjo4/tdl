# Third-party notices

TDL Desktop extends tdl, licensed under GNU Affero General Public License v3.0. The complete modified source is present in this repository. Original upstream: https://github.com/iyear/tdl ; requested fork: https://github.com/ej04520yjo4/tdl . See the root LICENSE.

The Windows desktop application bundles:

- Python 3.12: Python Software Foundation License. https://docs.python.org/3/license.html
- PySide6 / Qt for Python and Shiboken6 6.10.2: LGPLv3 / GPLv3 or commercial licensing as specified by the components. This AGPL desktop source uses the open-source distribution. https://doc.qt.io/qtforpython-6/licenses.html
- Qt 6 libraries: their applicable open-source licenses and third-party notices are distributed with PySide6. https://doc.qt.io/qt-6/licensing.html
- PyInstaller 6.19.0 bootloader: GPLv2 or later with the PyInstaller exception permitting bundled applications. https://pyinstaller.org/en/stable/license.html
- Go runtime and dependencies: see go.mod, go.sum, core/go.mod, core/go.sum, extension/go.mod, and extension/go.sum. Go uses a BSD-style license. https://go.dev/LICENSE

Build instructions and all application source are provided so the desktop executable can be rebuilt with modified application or library versions. The generated release license directory includes license texts from the installed packages and the Go module cache.
