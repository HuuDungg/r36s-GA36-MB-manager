#!/usr/bin/env python3
"""
Automated Build Script for R36S Manager.

This script:
1. Installs/verifies required Python packages (PySide6, Pillow, pyinstaller) in the virtual environment.
2. Locates the system's `mtools` installation and bundles its binary under the required names.
3. Packages the application using PyInstaller into a standalone macOS App Bundle.
4. Generates a macOS Disk Image (.dmg) installer.
"""

import sys
import os
import shutil
import subprocess

def log(msg):
    print(f"\n====> {msg}")

def locate_python_env():
    # Detect our virtual environment Python
    venv_python = os.path.abspath(".venv/bin/python")
    if not os.path.exists(venv_python):
        # Fallback to sys.executable if .venv not found (e.g. running in custom setups)
        venv_python = sys.executable
    return venv_python

def ensure_python_dependencies(python_bin):
    log("Verifying python dependencies in the environment...")
    
    # List of required packages
    requirements = ["PySide6", "Pillow", "pyinstaller"]
    
    for pkg in requirements:
        try:
            # Check if package is installed
            subprocess.run(
                [python_bin, "-c", f"import {pkg.lower() if pkg != 'PySide6' else 'PySide6'}"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print(f"  - {pkg} is already installed.")
        except subprocess.CalledProcessError:
            print(f"  - {pkg} not found. Installing...")
            subprocess.run([python_bin, "-m", "pip", "install", pkg], check=True)

def locate_mtools():
    log("Locating mtools...")
    
    # Try shutil.which
    mtools_path = shutil.which("mtools")
    if mtools_path:
        print(f"Found mtools in PATH: {mtools_path}")
        return mtools_path
        
    # Check common Homebrew paths
    brew_paths = [
        "/opt/homebrew/bin/mtools",      # Apple Silicon Mac
        "/usr/local/bin/mtools",        # Intel Mac
    ]
    for path in brew_paths:
        if os.path.exists(path):
            print(f"Found mtools at: {path}")
            return path
            
    # Try using brew prefix
    try:
        prefix = subprocess.check_output(["brew", "--prefix", "mtools"], text=True).strip()
        path = os.path.join(prefix, "bin", "mtools")
        if os.path.exists(path):
            print(f"Found mtools via brew --prefix: {path}")
            return path
    except Exception:
        pass
        
    # If not found, try installing it via brew
    log("mtools not found. Attempting to install via Homebrew...")
    try:
        subprocess.run(["brew", "install", "mtools"], check=True)
        mtools_path = shutil.which("mtools") or "/opt/homebrew/bin/mtools"
        if os.path.exists(mtools_path):
            print(f"Successfully installed and located mtools: {mtools_path}")
            return mtools_path
    except Exception as e:
        print(f"Failed to install mtools via Homebrew: {e}")
        
    raise FileNotFoundError("Could not locate or install 'mtools'. Please ensure Homebrew is installed and run 'brew install mtools' manually.")

def bundle_mtools(mtools_bin):
    log("Bundling mtools binaries...")
    mtools_dir = os.path.abspath("mtools_bin")
    if os.path.exists(mtools_dir):
        shutil.rmtree(mtools_dir)
    os.makedirs(mtools_dir)
    
    # The five commands required by the app
    commands = ["mdir", "mcopy", "mmd", "mdel", "mdeltree"]
    for cmd in commands:
        dst = os.path.join(mtools_dir, cmd)
        print(f"  - Copying {mtools_bin} -> {dst}")
        shutil.copy2(mtools_bin, dst)
        
    return mtools_dir

def build_app(python_bin, mtools_dir):
    log("Building application bundle with PyInstaller...")
    
    # Clean up previous builds
    for path in ["build", "dist"]:
        if os.path.exists(path):
            shutil.rmtree(path)
            
    # Locate pyinstaller executable in virtual env
    pyinstaller_bin = os.path.join(os.path.dirname(python_bin), "pyinstaller")
    if not os.path.exists(pyinstaller_bin):
        pyinstaller_bin = "pyinstaller"
        
    pyinstaller_cmd = [
        pyinstaller_bin,
        "--name=R36S_Manager",
        "--windowed",
        f"--add-data={mtools_dir}:mtools_bin",
        "--clean",
        "app.py"
    ]
    
    print(f"Running command: {' '.join(pyinstaller_cmd)}")
    subprocess.run(pyinstaller_cmd, check=True)
    
    app_path = os.path.abspath("dist/R36S_Manager.app")
    if os.path.exists(app_path):
        print(f"Successfully built App Bundle at: {app_path}")
        return app_path
    else:
        raise FileNotFoundError("Failed to locate built App Bundle after PyInstaller completed.")

def create_dmg(app_path):
    log("Creating DMG installer...")
    
    dmg_root = os.path.abspath("dist/dmg_root")
    if os.path.exists(dmg_root):
        shutil.rmtree(dmg_root)
    os.makedirs(dmg_root)
    
    # Copy .app bundle to dmg root
    dst_app = os.path.join(dmg_root, "R36S_Manager.app")
    print(f"  - Copying App Bundle to DMG folder...")
    shutil.copytree(app_path, dst_app, symlinks=True)
    
    # Create symlink to /Applications
    dst_apps_link = os.path.join(dmg_root, "Applications")
    print(f"  - Creating /Applications shortcut...")
    if os.path.exists(dst_apps_link):
        os.remove(dst_apps_link)
    os.symlink("/Applications", dst_apps_link)
    
    # Generate DMG
    dmg_path = os.path.abspath("dist/R36S_Manager.dmg")
    if os.path.exists(dmg_path):
        os.remove(dmg_path)
        
    dmg_cmd = [
        "hdiutil", "create",
        "-fs", "HFS+",
        "-volname", "R36S Manager",
        "-srcfolder", dmg_root,
        "-ov", dmg_path
    ]
    
    print(f"Running command: {' '.join(dmg_cmd)}")
    subprocess.run(dmg_cmd, check=True)
    
    # Clean up DMG root
    shutil.rmtree(dmg_root)
    
    print(f"\nSuccessfully created DMG installer at:\n  {dmg_path}")
    return dmg_path

def main():
    try:
        # 1. Locate python bin
        python_bin = locate_python_env()
        print(f"Using Python environment: {python_bin}")
        
        # 2. Ensure dependencies
        ensure_python_dependencies(python_bin)
        
        # 3. Locate mtools
        mtools_bin = locate_mtools()
        
        # 4. Bundle mtools
        mtools_dir = bundle_mtools(mtools_bin)
        
        # 5. Run PyInstaller build
        app_path = build_app(python_bin, mtools_dir)
        
        # 6. Create DMG package
        create_dmg(app_path)
        
        # Clean up temporary mtools_bin directory
        if os.path.exists(mtools_dir):
            shutil.rmtree(mtools_dir)
            
        log("Build completed successfully!")
        
    except Exception as e:
        log(f"BUILD FAILED: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
