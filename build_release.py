"""Automated build script for Audio AI Recorder & Transcriber.

Builds a lightweight standalone Windows executable using PyInstaller, prunes heavy
unused packages from the local environment, packages it into a zip archive, and
prepares the release asset for GitHub Releases.
"""

import os
import shutil
import subprocess
import sys
import zipfile

APP_NAME = "AudioTranscriber"
VERSION = "v1.0.0"
ZIP_NAME = f"{APP_NAME}-{VERSION}-windows-x64.zip"

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(ROOT_DIR, "dist")
BUILD_DIR = os.path.join(ROOT_DIR, "build")
APP_DIST_DIR = os.path.join(DIST_DIR, APP_NAME)
RELEASE_ZIP_PATH = os.path.join(DIST_DIR, ZIP_NAME)

# Heavy packages from local Python environment that are not used by the app
UNNEEDED_PACKAGES = [
    "torch",
    "torchvision",
    "torchaudio",
    "pandas",
    "pandas.libs",
    "sklearn",
    "matplotlib",
    "pyarrow",
    "pyarrow.libs",
    "jedi",
    "PIL",
    "grpc",
    "IPython",
    "notebook",
    "numba",
    "numba.libs",
    "llvmlite",
    "llvmlite.libs",
]


def clean():
    """Clean build artifacts."""
    print("Cleaning build directories...")
    for directory in (DIST_DIR, BUILD_DIR):
        if os.path.exists(directory):
            shutil.rmtree(directory, ignore_errors=True)
    spec_file = os.path.join(ROOT_DIR, f"{APP_NAME}.spec")
    if os.path.exists(spec_file):
        os.remove(spec_file)


def build_exe():
    """Run PyInstaller to build the standalone executable directory."""
    print("Building executable with PyInstaller...")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name", APP_NAME,
        "--windowed",  # No console window
        "--onedir",    # Directory mode for fast launch & robust DLL loading
        "--noconfirm",
        "--clean",
        "--collect-all", "pyaudiowpatch",
        "--collect-all", "soundfile",
        "--hidden-import", "scipy.signal",
        "main.py",
    ]

    for mod in UNNEEDED_PACKAGES:
        cmd.extend(["--exclude-module", mod])

    result = subprocess.run(cmd, cwd=ROOT_DIR)
    if result.returncode != 0:
        print("ERROR: PyInstaller build failed!")
        sys.exit(1)

    print(f"Executable built at: {APP_DIST_DIR}")


def prune_unneeded():
    """Prune any leftover heavy unneeded directories collected by PyInstaller hooks."""
    internal_dir = os.path.join(APP_DIST_DIR, "_internal")
    if not os.path.exists(internal_dir):
        return

    print("Pruning unneeded heavy packages from _internal...")
    for item in os.listdir(internal_dir):
        if any(item.startswith(pkg) for pkg in UNNEEDED_PACKAGES):
            target = os.path.join(internal_dir, item)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
                print(f"  - Pruned directory: {item}")
            elif os.path.isfile(target):
                os.remove(target)
                print(f"  - Pruned file: {item}")


def create_zip():
    """Package the built executable directory into a zip archive."""
    print(f"Creating zip archive: {RELEASE_ZIP_PATH}...")

    with zipfile.ZipFile(RELEASE_ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for root, dirs, files in os.walk(APP_DIST_DIR):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, DIST_DIR)
                zip_file.write(full_path, rel_path)

    zip_size_mb = os.path.getsize(RELEASE_ZIP_PATH) / (1024 * 1024)
    print(f"Release package created: {ZIP_NAME} ({zip_size_mb:.1f} MB)")


def main():
    clean()
    build_exe()
    prune_unneeded()
    create_zip()
    print("\n--- RELEASE BUILD COMPLETE ---")
    print(f"Zip archive ready for GitHub Release: {RELEASE_ZIP_PATH}")


if __name__ == "__main__":
    main()
