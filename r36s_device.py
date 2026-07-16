"""
R36S SD Card — Device detection and mtools wrapper.

Handles auto-detection of R36S SD cards on macOS, MBR parsing,
and wraps mtools (mdir/mcopy/mdel/mrd) for file operations on
hidden FAT32 partitions that macOS cannot mount natively.
"""

import subprocess
import struct
import re
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class FileEntry:
    """A file or directory on the SD card."""
    name: str
    is_dir: bool
    size: int = 0
    date: str = ""
    time: str = ""


@dataclass
class DeviceInfo:
    """Detected external disk information."""
    device_path: str        # e.g. /dev/disk4
    size_bytes: int = 0
    label: str = ""
    fat32_offset: int = 0   # byte offset of FAT32 game partition


# Map console directory names to friendly file-dialog filters
ROM_FILTERS = {
    "nes":        "NES ROMs (*.nes *.zip *.7z)",
    "famicom":    "NES ROMs (*.nes *.zip *.7z)",
    "snes":       "SNES ROMs (*.sfc *.smc *.zip *.7z)",
    "sfc":        "SNES ROMs (*.sfc *.smc *.zip *.7z)",
    "gb":         "Game Boy ROMs (*.gb *.zip *.7z)",
    "gbc":        "Game Boy Color ROMs (*.gbc *.zip *.7z)",
    "gba":        "GBA ROMs (*.gba *.zip *.7z)",
    "n64":        "N64 ROMs (*.z64 *.n64 *.v64 *.zip)",
    "nds":        "NDS ROMs (*.nds *.zip)",
    "psx":        "PS1 Games (*.bin *.cue *.pbp *.chd *.img)",
    "psp":        "PSP ISOs (*.iso *.cso)",
    "mame":       "Arcade ROMs (*.zip)",
    "arcade":     "Arcade ROMs (*.zip)",
    "genesis":    "Genesis ROMs (*.md *.bin *.zip)",
    "megadrive":  "Mega Drive ROMs (*.md *.bin *.zip)",
    "megaduck":   "Mega Duck ROMs (*.bin *.zip)",
    "neogeo":     "Neo Geo ROMs (*.zip)",
    "pcengine":   "PC Engine ROMs (*.pce *.zip)",
    "gamegear":   "Game Gear ROMs (*.gg *.zip)",
    "mastersystem": "Master System ROMs (*.sms *.zip)",
    "atari2600":  "Atari 2600 ROMs (*.a26 *.bin *.zip)",
    "atari7800":  "Atari 7800 ROMs (*.a78 *.bin *.zip)",
    "coleco":     "ColecoVision ROMs (*.col *.zip)",
    "dreamcast":  "Dreamcast Games (*.cdi *.gdi *.chd)",
    "saturn":     "Saturn Games (*.bin *.cue *.chd)",
    "segacd":     "Sega CD Games (*.bin *.cue *.chd)",
}


def format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    return f"{size_bytes / 1024 ** 3:.2f} GB"


class R36SDevice:
    """Manages communication with an R36S SD card via mtools."""

    def __init__(self):
        self.device: Optional[DeviceInfo] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected and self.device is not None

    # ------------------------------------------------------------------
    # Scanning & connecting
    # ------------------------------------------------------------------

    def scan_disks(self) -> list[DeviceInfo]:
        """Return a list of external physical disks."""
        result = subprocess.run(
            ["diskutil", "list"], capture_output=True, text=True
        )
        devices: list[DeviceInfo] = []
        cur_path: Optional[str] = None

        for line in result.stdout.splitlines():
            m = re.match(r"^(/dev/disk\d+)\s+\(external,\s*physical\):", line)
            if m:
                cur_path = m.group(1)
                continue
            if cur_path and re.search(r"^\s+#?0:", line):
                sm = re.search(r"\*?([\d.]+)\s+(KB|MB|GB|TB)", line)
                if sm:
                    val = float(sm.group(1))
                    mult = {"KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
                    size = int(val * mult.get(sm.group(2), 1))
                    devices.append(DeviceInfo(device_path=cur_path, size_bytes=size))
                cur_path = None
        return devices

    def _has_permission(self, device_path: str) -> bool:
        """Check if we can already read the block device."""
        try:
            with open(device_path, "rb") as f:
                f.read(1)
            return True
        except (PermissionError, OSError):
            return False

    def request_permissions(self, device_path: str) -> tuple[bool, str]:
        """
        Ensure we have read/write access to the block device.

        Returns (success, error_message).
        - First checks if permissions are already available.
        - If not, tries osascript (native macOS password dialog).
        - If osascript fails, returns instructions for manual chmod.
        """
        if self._has_permission(device_path):
            return True, ""

        # Try osascript
        raw = device_path.replace("disk", "rdisk")
        script = (
            f'do shell script "chmod 777 {device_path} {raw}" '
            f"with administrator privileges"
        )
        r = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True
        )
        if r.returncode == 0 and self._has_permission(device_path):
            return True, ""

        # Fallback: ask user to run sudo manually
        return False, (
            f"Cannot get permission automatically.\n\n"
            f"Please open Terminal and run:\n"
            f"  sudo chmod 777 {device_path} {raw}\n\n"
            f"Then click Refresh."
        )

    def detect_fat32_offset(self, device_path: str) -> int:
        """Read MBR, parse partition table, return byte offset of FAT32."""
        try:
            with open(device_path, "rb") as fh:
                mbr = fh.read(512)
        except (PermissionError, OSError):
            return 0
        if len(mbr) < 512:
            return 0

        # Fix MBR signature if needed
        sig = struct.unpack_from("<H", mbr, 510)[0]
        if sig != 0xAA55:
            self._fix_mbr(device_path)

        # Parse 4 partition entries starting at offset 446
        candidates = []
        for i in range(4):
            off = 446 + i * 16
            ptype = mbr[off + 4]
            lba = struct.unpack_from("<I", mbr, off + 8)[0]
            sectors = struct.unpack_from("<I", mbr, off + 12)[0]
            if ptype in (0x0B, 0x0C) and lba > 0:
                candidates.append((lba, sectors))

        if not candidates:
            return 0

        # Pick the largest FAT32 partition (the game partition)
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0] * 512

    def connect(self, device: DeviceInfo) -> tuple[bool, str]:
        """Connect to device: permissions → detect FAT32 → validate."""
        ok, err = self.request_permissions(device.device_path)
        if not ok:
            return False, err

        offset = self.detect_fat32_offset(device.device_path)
        if offset == 0:
            offset = 1732268032  # fallback: known R36S offset

        device.fat32_offset = offset

        # Validate with a quick mdir
        dev_str = f"{device.device_path}@@{offset}"
        try:
            r = subprocess.run(
                ["mdir", "-i", dev_str, "::/"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False, f"Cannot read SD card.\n{r.stderr.strip()}"
        except FileNotFoundError:
            return False, "mtools is not installed.\nRun: brew install mtools"
        except subprocess.TimeoutExpired:
            return False, "Timed out reading SD card."

        self.device = device
        self._connected = True
        return True, "Connected"

    def connect_image(self, img_path: str) -> tuple[bool, str]:
        """Connect to a local raw .img backup file instead of physical SD card."""
        if not os.path.isfile(img_path):
            return False, "File not found"

        sz = os.path.getsize(img_path)
        offset = self.detect_fat32_offset(img_path)
        if offset == 0:
            offset = 1732268032  # fallback

        # Validate with a quick mdir
        dev_str = f"{img_path}@@{offset}"
        try:
            r = subprocess.run(
                ["mdir", "-i", dev_str, "::/"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False, f"Cannot read disk image file.\n{r.stderr.strip()}"
        except FileNotFoundError:
            return False, "mtools is not installed.\nRun: brew install mtools"
        except subprocess.TimeoutExpired:
            return False, "Timed out reading disk image."

        self.device = DeviceInfo(
            device_path=img_path,
            size_bytes=sz,
            label="Disk Image",
            fat32_offset=offset
        )
        self._connected = True
        return True, "Connected"

    def disconnect(self):
        self._connected = False
        self.device = None

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def _dev(self) -> str:
        assert self.device
        return f"{self.device.device_path}@@{self.device.fat32_offset}"

    def file_exists(self, remote_path: str) -> bool:
        if not self.connected:
            return False
        remote = f"::/{remote_path.strip('/')}"
        r = subprocess.run(
            ["mdir", "-i", self._dev(), remote],
            capture_output=True
        )
        return r.returncode == 0

    def download_file(self, remote_path: str, local_path: str) -> bool:
        if not self.connected:
            return False
        remote = f"::/{remote_path.strip('/')}"
        r = subprocess.run(
            ["mcopy", "-i", self._dev(), remote, local_path],
            capture_output=True
        )
        return r.returncode == 0

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        if not self.connected:
            return False
        remote = f"::/{remote_path.strip('/')}"
        parent = os.path.dirname(remote_path.strip('/'))
        if parent:
            subprocess.run(["mmd", "-i", self._dev(), f"::/{parent}"], capture_output=True)
            
        r = subprocess.run(
            ["mcopy", "-i", self._dev(), "-o", local_path, remote],
            capture_output=True
        )
        return r.returncode == 0

    def delete_file(self, remote_path: str) -> bool:
        if not self.connected:
            return False
        remote = f"::/{remote_path.strip('/')}"
        r = subprocess.run(
            ["mdel", "-i", self._dev(), remote],
            capture_output=True
        )
        return r.returncode == 0

    def list_dir(self, path: str = "/") -> tuple[list[FileEntry], int]:
        """List directory contents. Returns (entries, free_bytes)."""
        if not self.connected:
            return [], 0

        remote = f"::/{path.strip('/')}" if path.strip("/") else "::"
        r = subprocess.run(
            ["mdir", "-i", self._dev(), remote + "/"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return [], 0

        entries: list[FileEntry] = []
        free_bytes = 0

        for line in r.stdout.splitlines():
            # Free space
            fm = re.search(r"([\d ]+)\s+bytes free", line)
            if fm:
                free_bytes = int(fm.group(1).replace(" ", ""))
                continue
            # Skip headers / summaries
            if (
                not line.strip()
                or line.lstrip().startswith("Volume")
                or line.lstrip().startswith("Directory")
                or re.match(r"^\s+\d+ files?\s", line)
            ):
                continue
            entry = self._parse_line(line)
            if entry:
                entries.append(entry)

        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries, free_bytes

    def copy_to(
        self, local_paths: list[str], remote_dir: str
    ) -> tuple[bool, str]:
        """Copy local files into *remote_dir* on the SD card."""
        if not self.connected:
            return False, "Not connected"

        remote = f"::/{remote_dir.strip('/')}/"
        errors: list[str] = []

        for p in local_paths:
            r = subprocess.run(
                ["mcopy", "-i", self._dev(), "-o", p, remote],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                errors.append(f"{os.path.basename(p)}: {r.stderr.strip()}")

        if errors:
            return False, "\n".join(errors)
        return True, f"Copied {len(local_paths)} file(s)"

    def delete_file(self, remote_path: str) -> tuple[bool, str]:
        if not self.connected:
            return False, "Not connected"
        remote = f"::/{remote_path.strip('/')}"
        r = subprocess.run(
            ["mdel", "-i", self._dev(), remote],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, "Deleted"

    def delete_dir(self, remote_path: str) -> tuple[bool, str]:
        """Recursively delete a directory using mdeltree."""
        if not self.connected:
            return False, "Not connected"

        remote = f"::/{remote_path.strip('/')}"
        r = subprocess.run(
            ["mdeltree", "-i", self._dev(), remote],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, "Deleted"

    def eject(self) -> tuple[bool, str]:
        if not self.device:
            return False, "No device"
        r = subprocess.run(
            ["diskutil", "eject", self.device.device_path],
            capture_output=True, text=True, timeout=10,
        )
        self._connected = False
        self.device = None
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, "Ejected safely"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fix_mbr(device_path: str):
        """Patch MBR signature + partition overlap byte."""
        try:
            subprocess.run(
                f"printf '\\x55\\xaa' | dd of={device_path} bs=1 seek=510 conv=notrunc",
                shell=True, capture_output=True,
            )
            subprocess.run(
                f"printf '\\x27' | dd of={device_path} bs=1 seek=492 conv=notrunc",
                shell=True, capture_output=True,
            )
        except Exception:
            pass

    @staticmethod
    def _parse_line(line: str) -> Optional[FileEntry]:
        """Parse one line of mdir output into a FileEntry."""
        line = line.rstrip()
        if not line:
            return None

        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d+:\d+)", line)
        if not dm:
            return None

        before = line[: dm.start()].rstrip()
        after = line[dm.end() :].strip()

        is_dir = "<DIR>" in before

        if is_dir:
            name_part = before.replace("<DIR>", "").strip()
            size = 0
        else:
            sm = re.search(r"(\d+)\s*$", before)
            if sm:
                size = int(sm.group(1))
                name_part = before[: sm.start()].strip()
            else:
                name_part = before.strip()
                size = 0

        display = after if after else name_part
        if display in (".", ".."):
            return None

        # If no long name, reconstruct from 8.3 parts
        if not after:
            parts = name_part.split()
            if len(parts) == 2 and not is_dir:
                display = f"{parts[0]}.{parts[1]}"
            elif parts:
                display = parts[0]

        return FileEntry(
            name=display, is_dir=is_dir, size=size,
            date=dm.group(1), time=dm.group(2),
        )
