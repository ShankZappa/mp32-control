#!/usr/bin/env python3
"""Gather third-party license texts so a binary build can ship them.

Source distribution is easy: the repository names its dependencies and carries none of
their code. A *binary* build is different — it bundles them, and two of the three have
terms that follow the binary:

* **pywebview** is BSD-3-Clause, which requires redistributions in binary form to
  reproduce the copyright notice, the conditions and the disclaimer "in the documentation
  and/or other materials provided with the distribution";
* **zeroconf** is LGPL-2.1-or-later, so its license must accompany the distribution;
* **PyInstaller's bootloader** is GPL-2.0-or-later with an exception that explicitly
  permits shipping it inside a program under any license — the notice still travels.

PyInstaller only carries a package's `dist-info` when that package is collected wholesale,
so relying on it is luck rather than a guarantee. This collects the texts explicitly and
fails loudly if one is missing, because a silent gap is exactly the kind that ships.

Writes into a staging directory that the build scripts add as `legal/third-party`.
"""
from __future__ import annotations

import importlib.metadata as md
import pathlib
import shutil
import sys

PACKAGES = ("pywebview", "zeroconf", "pyinstaller")
WANTED = ("LICENSE", "LICENCE", "COPYING", "NOTICE")


def collect(out_dir: pathlib.Path) -> int:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    missing = []
    for name in PACKAGES:
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            missing.append(f"{name} (not installed)")
            continue

        written = 0
        for entry in dist.files or []:
            stem = pathlib.Path(str(entry)).name.upper()
            if not any(w in stem for w in WANTED):
                continue
            src = pathlib.Path(dist.locate_file(entry))
            if not src.is_file():
                continue
            shutil.copyfile(src, out_dir / f"{name}-{src.name}")
            written += 1

        if written:
            print(f"  {name} {dist.version}: {written} license file(s)")
        else:
            missing.append(f"{name} {dist.version} (no license file in its metadata)")

    if missing:
        print("\nERROR: license texts could not be collected for:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print("\nA binary build must not be distributed without them. Reinstall the "
              "package from PyPI, or add the text by hand, then build again.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "build/legal-third-party")
    print("Collecting third-party license texts …")
    sys.exit(collect(target))
