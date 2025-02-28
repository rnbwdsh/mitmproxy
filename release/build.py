#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal

import click
import cryptography.fernet

here = Path(__file__).absolute().parent

TEMP_DIR = here / "build"
DIST_DIR = here / "dist"


@click.group(chain=True)
@click.option("--dirty", is_flag=True)
def cli(dirty):
    if dirty:
        print("Keeping temporary files.")
    else:
        print("Cleaning up temporary files...")
        if TEMP_DIR.exists():
            shutil.rmtree(TEMP_DIR)
        if DIST_DIR.exists():
            shutil.rmtree(DIST_DIR)

        TEMP_DIR.mkdir()
        DIST_DIR.mkdir()


@cli.command()
def wheel():
    """Build the wheel for PyPI."""
    print("Building wheel...")
    subprocess.check_call(
        [
            "python",
            "setup.py",
            "-q",
            "bdist_wheel",
            "--dist-dir",
            DIST_DIR,
        ]
    )
    (whl,) = DIST_DIR.glob("mitmproxy-*-py3-none-any.whl")
    print(f"Found wheel package: {whl}")
    subprocess.check_call(["tox", "-e", "wheeltest", "--", whl])


class ZipFile2(zipfile.ZipFile):
    # ZipFile and tarfile have slightly different APIs. Let's fix that.
    def add(self, name: str, arcname: str) -> None:
        return self.write(name, arcname)

    def __enter__(self) -> ZipFile2:
        return self

    @property
    def name(self) -> str:
        assert self.filename
        return self.filename


def archive(path: Path) -> tarfile.TarFile | ZipFile2:
    if platform.system() == "Windows":
        return ZipFile2(path.with_suffix(".zip"), "w")
    else:
        return tarfile.open(path.with_suffix(".tar.gz"), "w:gz")


def version() -> str:
    if ref := os.environ.get("GITHUB_REF", ""):
        if ref.startswith("refs/heads/"):
            return ref.replace("refs/heads/", "")
        if ref.startswith("refs/tags/"):
            return ref.replace("refs/tags/", "")
    return os.environ.get("BUILD_VERSION", "dev")


def operating_system() -> Literal["windows", "linux", "macos", "unknown"]:
    pf = platform.system()
    if pf == "Windows":
        return "windows"
    elif pf == "Linux":
        return "linux"
    elif pf == "Darwin":
        return "macos"
    else:
        return "unknown"


def _pyinstaller(specfile: str) -> None:
    print(f"Invoking PyInstaller with {specfile}...")
    subprocess.check_call(
        [
            "pyinstaller",
            "--clean",
            "--workpath",
            TEMP_DIR / "pyinstaller/temp",
            "--distpath",
            TEMP_DIR / "pyinstaller/dist",
            specfile,
        ],
        cwd=here / "specs",
    )


@cli.command()
def standalone_binaries():
    """All platforms: Build the standalone binaries generated with PyInstaller"""
    with archive(DIST_DIR / f"mitmproxy-{version()}-{operating_system()}") as f:
        _pyinstaller("standalone.spec")

        for tool in ["mitmproxy", "mitmdump", "mitmweb"]:
            executable = TEMP_DIR / "pyinstaller/dist" / tool
            if platform.system() == "Windows":
                executable = executable.with_suffix(".exe")

            # Test if it works at all O:-)
            print(f"> {executable} --version")
            subprocess.check_call([executable, "--version"])

            f.add(str(executable), str(executable.name))
    print(f"Packed {f.name}.")


def _ensure_pyinstaller_onedir():
    if not (TEMP_DIR / "pyinstaller/dist/onedir").exists():
        _pyinstaller("windows-dir.spec")

    for tool in ["mitmproxy", "mitmdump", "mitmweb"]:
        print(f"> {tool} --version")
        executable = (TEMP_DIR / "pyinstaller/dist/onedir" / tool).with_suffix(".exe")
        subprocess.check_call([executable, "--version"])


@cli.command()
def msix_installer():
    """Windows: Build the MSIX installer for the Windows Store."""
    _ensure_pyinstaller_onedir()

    shutil.copytree(
        TEMP_DIR / "pyinstaller/dist/onedir",
        TEMP_DIR / "msix",
        dirs_exist_ok=True,
    )
    shutil.copytree(here / "windows-installer", TEMP_DIR / "msix", dirs_exist_ok=True)

    manifest = TEMP_DIR / "msix/AppxManifest.xml"
    app_version = version()
    if not re.match(r"\d+\.\d+\.\d+", app_version):
        app_version = datetime.now().strftime("%y%m.%d.%H%M").replace(".0", ".").replace(".0", ".").replace(".0", ".")
    manifest.write_text(manifest.read_text().replace("1.2.3", app_version))

    makeappx_exe = (
        Path(os.environ["ProgramFiles(x86)"])
        / "Windows Kits/10/App Certification Kit/makeappx.exe"
    )
    subprocess.check_call(
        [
            makeappx_exe,
            "pack",
            "/d",
            TEMP_DIR / "msix",
            "/p",
            DIST_DIR / f"mitmproxy-{version()}-installer.msix",
        ],
    )
    assert (DIST_DIR / f"mitmproxy-{version()}-installer.msix").exists()


@cli.command()
def installbuilder_installer():
    """Windows: Build the InstallBuilder installer."""
    _ensure_pyinstaller_onedir()

    IB_VERSION = "21.6.0"
    IB_SETUP_SHA256 = "2bc9f9945cb727ad176aa31fa2fa5a8c57a975bad879c169b93e312af9d05814"
    IB_DIR = here / "installbuilder"
    IB_SETUP = IB_DIR / "setup" / f"{IB_VERSION}-installer.exe"
    IB_CLI = Path(
        rf"C:\Program Files\VMware InstallBuilder Enterprise {IB_VERSION}\bin\builder-cli.exe"
    )
    IB_LICENSE = IB_DIR / "license.xml"

    if not IB_LICENSE.exists():
        print("Decrypt InstallBuilder license...")
        f = cryptography.fernet.Fernet(os.environ["CI_BUILD_KEY"].encode())
        with open(IB_LICENSE.with_suffix(".xml.enc"), "rb") as infile, open(
            IB_LICENSE, "wb"
        ) as outfile:
            outfile.write(f.decrypt(infile.read()))

    if not IB_CLI.exists():
        if not IB_SETUP.exists():
            print("Downloading InstallBuilder...")

            def report(block, blocksize, total):
                done = block * blocksize
                if round(100 * done / total) != round(100 * (done - blocksize) / total):
                    print(f"Downloading... {round(100 * done / total)}%")

            tmp = IB_SETUP.with_suffix(".tmp")
            urllib.request.urlretrieve(
                f"https://clients.bitrock.com/installbuilder/installbuilder-enterprise-{IB_VERSION}-windows-x64-installer.exe",
                tmp,
                reporthook=report,
            )
            tmp.rename(IB_SETUP)

        ib_setup_hash = hashlib.sha256()
        with IB_SETUP.open("rb") as fp:
            while True:
                data = fp.read(65_536)
                if not data:
                    break
                ib_setup_hash.update(data)
        if ib_setup_hash.hexdigest() != IB_SETUP_SHA256:  # pragma: no cover
            raise RuntimeError("InstallBuilder hashes don't match.")

        print("Install InstallBuilder...")
        subprocess.run(
            [IB_SETUP, "--mode", "unattended", "--unattendedmodeui", "none"], check=True
        )
        assert IB_CLI.is_file()

    print("Run InstallBuilder...")
    subprocess.check_call(
        [
            IB_CLI,
            "build",
            str(IB_DIR / "mitmproxy.xml"),
            "windows-x64",
            "--license",
            str(IB_LICENSE),
            "--setvars",
            f"project.version={version()}",
            "--verbose",
        ]
    )
    assert (DIST_DIR / f"mitmproxy-{version()}-windows-x64-installer.exe").exists()


if __name__ == "__main__":
    cli()
