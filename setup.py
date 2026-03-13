from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.develop import develop
from setuptools.command.install import install


def _run_post_install_hook() -> None:
    hook_path = Path(__file__).resolve().parent / "install_hooks.py"
    if hook_path.exists():
        subprocess.call([sys.executable, str(hook_path)])


class PostInstall(install):
    def run(self) -> None:
        super().run()
        _run_post_install_hook()


class PostDevelop(develop):
    def run(self) -> None:
        super().run()
        _run_post_install_hook()


setup(
    cmdclass={
        "install": PostInstall,
        "develop": PostDevelop,
    }
)