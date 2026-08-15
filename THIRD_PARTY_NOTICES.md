# Third-party software notices

MP32 Control's original source code is licensed under MIT. Its standalone builds also
bundle third-party Python software. The principal direct dependencies are:

- **pywebview** — BSD 3-Clause License, copyright Roman Sirokov and contributors.
  Project: <https://github.com/r0x0r/pywebview>
- **zeroconf** — GNU Lesser General Public License 2.1 or later.
  Project: <https://github.com/python-zeroconf/python-zeroconf>
- **PyInstaller bootloader** — GNU GPL 2.0 or later with the PyInstaller Bootloader
  Exception, which permits distributing the bootloader as part of the bundled app.
  Project and license terms: <https://github.com/pyinstaller/pyinstaller>

The exact dependency versions used for a release are resolved from `requirements.txt`
at build time. These packages may bring transitive dependencies, whose license metadata
is distributed with them where the packaging tool includes it. Distributors should
preserve this notice, the application's `legal` directory, and all bundled package
license metadata.
