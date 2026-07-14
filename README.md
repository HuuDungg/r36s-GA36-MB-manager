# WorkaholicSEA — R36S Manager

![App Screenshot](assets/screenshot.png)

A native macOS desktop application designed to manage game files (ROMs) on R36S retro handheld SD cards. 

Because macOS natively blocks modifying FAT32/exFAT partitions that are located behind Ext4/Linux partitions (a common layout for R36S SD cards running ArkOS/AmberELEC), managing games via standard Finder is typically impossible. This application bridges that gap by directly parsing the Master Boot Record (MBR) and utilizing `mtools` to read, write, and manage files without requiring macOS to mount the partition.

## Features
- **Native UI**: Built with PySide6 (Qt) for a clean, native macOS look and feel.
- **Direct Hardware Access**: Reads raw block devices (`/dev/rdiskX`) to bypass macOS filesystem mounting restrictions.
- **Auto-Detection**: Scans physical disks and automatically identifies the R36S FAT32/EASYROMS partition via partition table offset calculations.
- **File Management**: 
  - Drag and drop ROMs directly into the application.
  - Delete unwanted games easily.
  - Sidebar with quick-search filtering for consoles (e.g., gba, snes, psx).
  - Breadcrumb navigation (Back/Forward/Up).
- **One-Click Launcher**: A `.command` file is provided for easy launching via Finder.

## Prerequisites
- macOS
- Python 3.9+
- `mtools` installed via Homebrew

## Installation

1. **Install Dependencies:**
   ```bash
   brew install mtools
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Permissions:**
   Because the application reads raw disk blocks, it requires administrative privileges to access `/dev/diskX`. The application will instruct you on how to temporarily grant `chmod 777` permissions to your SD card when connected.

## Usage
Simply double-click `Start_R36S_Manager.command` to launch the application.

- **Refresh**: Scans for connected SD cards.
- **Add Game**: Opens a file dialog or allows drag-and-drop.
- **Delete**: Removes the selected ROM.
- **Eject**: Safely unmounts and ejects the SD card from the system.

## Architecture
- `app.py`: Contains the PySide6 UI logic, state management, and file drag-and-drop operations.
- `r36s_device.py`: The core driver layer that handles raw disk scanning, MBR offset parsing, and wraps `mtools` commands (`mcopy`, `mdel`, `mdir`) for seamless read/write operations.
