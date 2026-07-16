"""
Boot partition editor for R36S .img backup files.

Reads and modifies boot assets (logo, battery animation BMPs) directly
from/to disk-image files.  Uses hdiutil + mount on macOS to access the
FAT16 boot partition; Pillow to convert imported images.
"""

import io
import os
import struct
import subprocess
import tempfile
import shutil
import re
from dataclasses import dataclass
from typing import Optional

from PIL import Image as PILImage
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtCore import Qt


# ======================================================================
# Data classes
# ======================================================================

@dataclass
class BootAsset:
    """One BMP image from the boot partition."""
    name: str            # relative path, e.g. "bootlogo.bmp"
    display_name: str    # human-friendly, e.g. "Boot Logo"
    category: str        # "logo" | "battery"
    width: int
    height: int
    bpp: int             # bits per pixel (usually 24)
    size_bytes: int
    original_data: bytes
    modified_data: Optional[bytes] = None

    @property
    def is_modified(self) -> bool:
        return self.modified_data is not None

    @property
    def current_data(self) -> bytes:
        return self.modified_data if self.modified_data is not None else self.original_data


@dataclass
class ImageInfo:
    """Metadata extracted from the .img file."""
    path: str
    size_bytes: int
    firmware_name: str = "Unknown Firmware"
    boot_offset_sectors: int = 0
    boot_size_sectors: int = 0


# ── Display name table ───────────────────────────────────────────────

BATTERY_NAMES = {
    "bat0": "Pin 0%",   "bat1": "Pin 10%",  "bat2": "Pin 20%",
    "bat3": "Pin 30%",  "bat4": "Pin 40%",  "bat5": "Pin 50%",
    "bat6": "Pin 60%",  "bat7": "Pin 70%",  "bat8": "Pin 80%",
    "bat9": "Pin 90%",  "bat10": "Pin 100%",
    "battery": "Battery Icon",
    "battery_charge": "Đang Sạc",
    "bempty": "Pin Cạn",
    "low_pwr": "Pin Yếu",
}


# ======================================================================
# Editor
# ======================================================================

class BootPartitionEditor:
    """
    Read / replace / save boot-partition BMP assets in an R36S .img file.

    Usage::

        ed = BootPartitionEditor()
        ok, msg = ed.load_image("/path/to/backup.img")
        ed.replace_asset("bootlogo.bmp", "/path/to/new_logo.png")
        ed.save_to_image()
    """

    SECTOR = 512
    # BPB says 262 144 sectors; hdiutil needs this much to mount
    MOUNT_SECTORS = 262_144   # 128 MB extracted for mounting

    # ── public API ───────────────────────────────────────────────

    def load_image(self, path: str) -> tuple[bool, str]:
        """Open *path*, find the FAT16 boot partition, read all BMP assets."""
        if not os.path.exists(path):
            return False, f"File not found: {path}"

        is_device = path.startswith("/dev/")
        if is_device:
            # Block devices report size 0 via os.path.getsize on macOS
            # Use diskutil to get actual size
            try:
                r = subprocess.run(
                    ["diskutil", "info", "-plist", path],
                    capture_output=True, timeout=10
                )
                import plistlib
                plist = plistlib.loads(r.stdout)
                sz = plist.get("TotalSize", 0) or plist.get("Size", 0)
            except Exception:
                sz = 0
            if sz < 50_000_000:
                return False, "Device too small or could not determine size."
        else:
            sz = os.path.getsize(path)
            if sz < 50_000_000:
                return False, "File too small to be a valid R36S image."

        # For block devices, use raw device for faster I/O
        read_path = path.replace("/dev/disk", "/dev/rdisk") if is_device else path

        off, cnt = self._find_fat16(read_path)
        if off == 0:
            return False, "No FAT16 boot partition found."

        self.info = ImageInfo(
            path=path, size_bytes=sz,
            firmware_name=self._scan_fw_name(read_path),
            boot_offset_sectors=off, boot_size_sectors=cnt,
        )

        try:
            self._mount_and_read(path, off)
            self._read_easyroms_assets(path)
        except Exception as exc:
            return False, f"Could not read boot assets:\n{exc}"

        if not self.assets:
            return False, "No BMP assets found in boot partition."

        return True, f"Loaded {len(self.assets)} assets"

    def ensure_asset_data(self, name: str, force_original: bool = False) -> bytes:
        asset = self.assets.get(name)
        if not asset:
            return b""
        if not force_original and asset.modified_data is not None:
            return asset.modified_data
        if asset.original_data is not None:
            return asset.original_data
            
        # Load on-demand from EASYROMS partition
        if name.startswith("themes/"):
            dev_str = f"{self.info.path}@@1732268032"
            td = tempfile.gettempdir()
            local_tmp = os.path.join(td, f"r36s_lazy_{os.path.basename(name)}")
            try:
                rc = subprocess.run(["mcopy", "-i", dev_str, f"::/{name}", local_tmp], capture_output=True)
                if rc.returncode == 0 and os.path.isfile(local_tmp):
                    with open(local_tmp, 'rb') as f:
                        asset.original_data = f.read()
            finally:
                try:
                    os.remove(local_tmp)
                except:
                    pass
                    
        return asset.original_data if asset.original_data is not None else b""

    def get_pixmap(
        self, name: str, *, modified: bool = True, max_w: int = 0,
    ) -> QPixmap:
        """Convert the named asset's BMP data to a ``QPixmap``, with caching."""
        asset = self.assets.get(name)
        if not asset:
            return QPixmap()
        
        # Check cache first
        cache_key = (name, modified, max_w)
        data_ref = id(asset.modified_data) if modified and asset.modified_data else id(asset.original_data)
        cached = self._pixmap_cache.get(cache_key)
        if cached and cached[0] == data_ref:
            return cached[1]
        
        raw = self.ensure_asset_data(name, force_original=not modified)
        if not raw:
            return QPixmap()
        
        # Non-image assets
        if any(name.endswith(ext) for ext in (".mp4", ".mp3", ".ogg", ".wav", ".ascii")):
            return QPixmap()
            
        # Use Pillow to convert to PNG first
        try:
            from PIL import Image as PILImage
            import io
            pil_img = PILImage.open(io.BytesIO(raw)).convert("RGB")
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            qimg = QImage.fromData(buf.getvalue())
        except Exception as exc:
            qimg = QImage.fromData(raw)
            
        if qimg.isNull():
            return QPixmap()
        pm = QPixmap.fromImage(qimg)
        if max_w and pm.width() > max_w:
            pm = pm.scaledToWidth(max_w, Qt.SmoothTransformation)
        
        # Store in cache
        self._pixmap_cache[cache_key] = (data_ref, pm)
        return pm

    def replace_asset(self, name: str, src_path: str) -> tuple[bool, str]:
        """Import *src_path* as a replacement, auto-converting to BMP."""
        print(f"[DEBUG] replace_asset: name={name}, src_path={src_path}")
        asset = self.assets.get(name)
        if not asset:
            print(f"[DEBUG] replace_asset: unknown asset {name}")
            return False, f"Unknown asset: {name}"
        if not os.path.isfile(src_path):
            print(f"[DEBUG] replace_asset: source file not found: {src_path}")
            return False, "Source file not found."
        try:
            if name.startswith("splash/") or name.startswith("launchimages/") or name.startswith("themes/"):
                print(f"[DEBUG] replace_asset: raw replacement (no conversion)")
                with open(src_path, 'rb') as f:
                    asset.modified_data = f.read()
                
                # Auto-delete conflicting video/gif if setting static image
                if name == "launchimages/loading.jpg":
                    for ext in (".mp4", ".gif", ".ascii"):
                        conf = self.assets.get(f"launchimages/loading{ext}")
                        if conf: conf.modified_data = b""
                elif name == "splash/splash.png":
                    conf = self.assets.get("splash/splash.mp4")
                    if conf: conf.modified_data = b""
                    
                print(f"[DEBUG] replace_asset: raw replacement success")
                return True, f"Replaced {asset.display_name}"

            print(f"[DEBUG] replace_asset: converting to BMP ({asset.width}x{asset.height})")
            bmp = self._convert_bmp(src_path, asset.width, asset.height)
            asset.modified_data = bmp
            
            if name == "bootlogo.bmp":
                splash = self.assets.get("splash/splash.png")
                if splash:
                    from PIL import Image as PILImage
                    import io
                    pil_img = PILImage.open(src_path).convert("RGB")
                    buf = io.BytesIO()
                    pil_img.save(buf, format="PNG")
                    splash.modified_data = buf.getvalue()
                mp4 = self.assets.get("splash/splash.mp4")
                if mp4: mp4.modified_data = b""
                
            print(f"[DEBUG] replace_asset: BMP conversion success")
            return True, f"Replaced {asset.display_name}"
        except Exception as exc:
            print(f"[DEBUG] replace_asset: exception occurred: {exc}")
            return False, str(exc)

    def reset_asset(self, name: str):
        a = self.assets.get(name)
        if a:
            a.modified_data = None
            if name == "launchimages/loading.jpg":
                for ext in (".mp4", ".gif", ".ascii"):
                    conf = self.assets.get(f"launchimages/loading{ext}")
                    if conf: conf.modified_data = None
            elif name in ("bootlogo.bmp", "splash/splash.png"):
                conf_png = self.assets.get("splash/splash.png")
                if conf_png: conf_png.modified_data = None
                conf_mp4 = self.assets.get("splash/splash.mp4")
                if conf_mp4: conf_mp4.modified_data = None

    def reset_all(self):
        for a in self.assets.values():
            a.modified_data = None

    def has_changes(self) -> bool:
        return any(a.is_modified for a in self.assets.values())

    def changes_count(self) -> int:
        return sum(1 for a in self.assets.values() if a.is_modified)

    def save_to_image(self) -> tuple[bool, str]:
        """Write every pending modification back into the .img file."""
        if not self.info:
            return False, "No image loaded."
        if not self.has_changes():
            return True, "Nothing to save."

        img_path = self.info.path
        byte_off = self.info.boot_offset_sectors * self.SECTOR
        mount_bytes = self.MOUNT_SECTORS * self.SECTOR
        write_bytes = self.info.boot_size_sectors * self.SECTOR

        td = tempfile.mkdtemp(prefix="r36s_save_")
        tmp = os.path.join(td, "boot.img")
        mnt = os.path.join(td, "mnt")
        os.makedirs(mnt)
        did = None
        n = 0

        try:
            # 1. Check if we have boot partition modifications (only bootlogo.bmp and bat/*.bmp)
            boot_assets_to_save = [
                a for a in self.assets.values()
                if a.is_modified and not any(
                    a.name.startswith(prefix)
                    for prefix in ("splash/", "launchimages/", "themes/", "rootfs/")
                )
            ]
            
            print(f"[DEBUG] save_to_image: {len(boot_assets_to_save)} boot assets to save")
            if boot_assets_to_save:
                is_device = img_path.startswith("/dev/")
                read_path = img_path.replace("/dev/disk", "/dev/rdisk") if is_device else img_path

                # extract boot
                print(f"[DEBUG] save_to_image: extracting boot partition")
                with open(read_path, "rb") as f:
                    f.seek(byte_off)
                    part = f.read(mount_bytes)
                with open(tmp, "wb") as f:
                    f.write(part)

                # mount
                print(f"[DEBUG] save_to_image: attaching boot image")
                did = self._attach(tmp)
                if not did:
                    return False, "hdiutil attach failed (save)."
                print(f"[DEBUG] save_to_image: mounting boot partition")
                if not self._mnt(did, mnt):
                    return False, "mount -t msdos failed (save)."

                # write modified files
                for a in boot_assets_to_save:
                    print(f"[DEBUG] save_to_image: writing boot asset {a.name}")
                    dst = os.path.join(mnt, a.name)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "wb") as f:
                        f.write(a.modified_data)
                    n += 1

                # unmount (flushes)
                print(f"[DEBUG] save_to_image: unmounting boot partition")
                self._umnt(mnt)
                self._detach(did)
                did = None

                # read back only MBR-defined partition size
                with open(tmp, "rb") as f:
                    modified = f.read(write_bytes)

                # patch .img boot partition area
                print(f"[DEBUG] save_to_image: patching boot partition in image")
                with open(read_path, "r+b") as f:
                    f.seek(byte_off)
                    f.write(modified)

            # 2. Check if we have EASYROMS modifications (splash, launchimages, themes)
            easyroms_assets_to_save = [
                a for a in self.assets.values()
                if a.is_modified and (
                    a.name.startswith("splash/") or
                    a.name.startswith("launchimages/") or
                    a.name.startswith("themes/")
                )
            ]
            print(f"[DEBUG] save_to_image: {len(easyroms_assets_to_save)} EASYROMS assets to save")
            if easyroms_assets_to_save:
                dev_str = f"{img_path}@@1732268032"
                
                for a in easyroms_assets_to_save:
                    print(f"[DEBUG] save_to_image: writing EASYROMS asset {a.name}")
                    
                    if a.modified_data:
                        # Write file via temp file and mcopy
                        with tempfile.NamedTemporaryFile(delete=False) as tf:
                            tf.write(a.modified_data)
                            tmp_path = tf.name
                        try:
                            print(f"[DEBUG] save_to_image: mcopy writing {len(a.modified_data)} bytes to ::/{a.name}")
                            r = subprocess.run(
                                ["mcopy", "-i", dev_str, "-o", tmp_path, f"::/{a.name}"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
                            )
                            print(f"[DEBUG] save_to_image: mcopy rc={r.returncode}")
                        finally:
                            try:
                                os.remove(tmp_path)
                            except:
                                pass
                    else:
                        # Delete file
                        print(f"[DEBUG] save_to_image: mdel deleting ::/{a.name}")
                        r = subprocess.run(
                            ["mdel", "-i", dev_str, f"::/{a.name}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
                        )
                        print(f"[DEBUG] save_to_image: mdel rc={r.returncode}")
                    n += 1

            # 3. Check if we have RootFS modifications
            rootfs_assets_to_save = [a for a in self.assets.values() if a.is_modified and a.name.startswith("rootfs/")]
            print(f"[DEBUG] save_to_image: {len(rootfs_assets_to_save)} RootFS assets to save")
            if rootfs_assets_to_save:
                dev_str = f"{img_path}@@121634816"
                for a in rootfs_assets_to_save:
                    print(f"[DEBUG] save_to_image: writing RootFS asset {a.name}")
                    short_name = a.name.replace("rootfs/", "")
                    if a.modified_data:
                        with tempfile.NamedTemporaryFile(delete=False) as tf:
                            tf.write(a.modified_data)
                            tmp_path = tf.name
                        try:
                            r = subprocess.run(
                                ["mcopy", "-i", dev_str, "-o", tmp_path, f"::/{short_name}"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
                            )
                            print(f"[DEBUG] save_to_image: mcopy rc={r.returncode}")
                        finally:
                            try:
                                os.remove(tmp_path)
                            except:
                                pass
                    else:
                        r = subprocess.run(
                            ["mdel", "-i", dev_str, f"::/{short_name}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
                        )
                        print(f"[DEBUG] save_to_image: mdel rc={r.returncode}")
                    n += 1

            # commit: current → original
            for a in self.assets.values():
                if a.is_modified:
                    a.original_data = a.modified_data
                    a.modified_data = None

            print(f"[DEBUG] save_to_image: done, saved {n} change(s)")
            return True, f"Saved {n} change(s) to {os.path.basename(img_path)}"

        except Exception as exc:
            print(f"[DEBUG] save_to_image: exception: {exc}")
            return False, f"Save failed: {exc}"
        finally:
            self._umnt(mnt)
            if did:
                self._detach(did)
            shutil.rmtree(td, ignore_errors=True)

    # ── init ─────────────────────────────────────────────────────

    def __init__(self):
        self.info: Optional[ImageInfo] = None
        self.assets: dict[str, BootAsset] = {}
        self._pixmap_cache: dict = {}  # (name, modified, max_w) -> (data_id, QPixmap)

    # ── MBR parsing ──────────────────────────────────────────────

    @staticmethod
    def _find_fat16(path: str) -> tuple[int, int]:
        """Return (start_sector, sector_count) for the first FAT16 entry."""
        with open(path, "rb") as f:
            mbr = f.read(512)
        for i in range(4):
            o = 446 + i * 16
            pt = mbr[o + 4]
            lba = struct.unpack_from("<I", mbr, o + 8)[0]
            sec = struct.unpack_from("<I", mbr, o + 12)[0]
            if pt in (0x04, 0x06, 0x0E) and lba > 0:
                return lba, sec
        return 0, 0

    @staticmethod
    def _scan_fw_name(path: str) -> str:
        """Best-effort firmware name search (first 10 MB)."""
        try:
            with open(path, "rb") as f:
                for off in range(0, 10 * 1024 * 1024, 1024 * 1024):
                    f.seek(off)
                    chunk = f.read(1024 * 1024)
                    idx = chunk.find(b"GA36")
                    if idx < 0:
                        continue
                    end = chunk.find(b"\x00", idx)
                    if end < 0 or end - idx > 80:
                        end = idx + 60
                    s = chunk[idx:end].decode("ascii", errors="ignore")
                    s = s.split("\n")[0].split("\r")[0].strip()
                    s = "".join(c for c in s if c.isprintable())
                    if len(s) > 5:
                        return s
        except Exception:
            pass
        return "Unknown Firmware"

    # ── mount helpers ────────────────────────────────────────────

    def _mount_and_read(self, img_path: str, boot_off: int):
        """Extract boot partition → mount → read BMPs → cleanup."""
        self.assets.clear()
        td = tempfile.mkdtemp(prefix="r36s_rd_")
        tmp = os.path.join(td, "boot.img")
        mnt = os.path.join(td, "mnt")
        os.makedirs(mnt)
        did = None
        is_device = img_path.startswith("/dev/")
        read_path = img_path.replace("/dev/disk", "/dev/rdisk") if is_device else img_path
        try:
            with open(read_path, "rb") as f:
                f.seek(boot_off * self.SECTOR)
                data = f.read(self.MOUNT_SECTORS * self.SECTOR)
            with open(tmp, "wb") as f:
                f.write(data)

            did = self._attach(tmp)
            if not did:
                raise RuntimeError(
                    "hdiutil attach -nomount failed.\n"
                    "The image may be corrupt or missing a boot partition."
                )
            if not self._mnt(did, mnt):
                raise RuntimeError(
                    "mount -t msdos failed.\n"
                    "The boot partition may not be FAT16."
                )

            # ── read BMP assets ──
            bl = os.path.join(mnt, "bootlogo.bmp")
            if os.path.isfile(bl):
                self._ingest(bl, "bootlogo.bmp", "Boot Logo", "logo")

            bd = os.path.join(mnt, "bat")
            if os.path.isdir(bd):
                for fn in sorted(os.listdir(bd)):
                    if fn.lower().endswith(".bmp"):
                        key = fn[:-4]
                        dn = BATTERY_NAMES.get(key, key)
                        self._ingest(
                            os.path.join(bd, fn),
                            f"bat/{fn}", dn, "battery",
                        )
        finally:
            self._umnt(mnt)
            if did:
                self._detach(did)
            shutil.rmtree(td, ignore_errors=True)

    def _read_easyroms_assets(self, img_path: str):
        # EASYROMS offset is 1732268032 bytes (3383336 sectors)
        dev_str = f"{img_path}@@1732268032"
        td = tempfile.mkdtemp(prefix="r36s_splash_")
        try:
            # 1. Try to read splash.png
            png_tmp = os.path.join(td, "splash.png")
            r = subprocess.run(
                ["mcopy", "-i", dev_str, "::/splash/splash.png", png_tmp],
                capture_output=True, text=True, timeout=10
            )
            png_data = b""
            if r.returncode == 0 and os.path.isfile(png_tmp):
                with open(png_tmp, 'rb') as f:
                    png_data = f.read()
            
            # Create splash.png asset (even if empty, we list it so users can set it)
            self.assets["splash/splash.png"] = BootAsset(
                name="splash/splash.png", display_name="Loading Splash Image", category="splash",
                width=640, height=480, bpp=24, size_bytes=len(png_data),
                original_data=png_data
            )
            
            # 2. Try to read splash.mp4
            mp4_tmp = os.path.join(td, "splash.mp4")
            r = subprocess.run(
                ["mcopy", "-i", dev_str, "::/splash/splash.mp4", mp4_tmp],
                capture_output=True, text=True, timeout=10
            )
            mp4_data = b""
            if r.returncode == 0 and os.path.isfile(mp4_tmp):
                with open(mp4_tmp, 'rb') as f:
                    mp4_data = f.read()
            
            # Create splash.mp4 asset
            self.assets["splash/splash.mp4"] = BootAsset(
                name="splash/splash.mp4", display_name="Loading Splash Video", category="splash",
                width=0, height=0, bpp=0, size_bytes=len(mp4_data),
                original_data=mp4_data
            )

            # 3. Try to read rootfs/low_pwr.bmp (Partition 7 RootFS offset: 121634816 bytes)
            rootfs_str = f"{img_path}@@121634816"
            low_pwr_tmp = os.path.join(td, "low_pwr.bmp")
            r = subprocess.run(
                ["mcopy", "-i", rootfs_str, "::/low_pwr.bmp", low_pwr_tmp],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and os.path.isfile(low_pwr_tmp):
                self._ingest(low_pwr_tmp, "rootfs/low_pwr.bmp", "Low Power Warning Screen", "rootfs")

            # 4. Try to read launchimages/loading.gif
            gif_tmp = os.path.join(td, "loading.gif")
            r = subprocess.run(
                ["mcopy", "-i", dev_str, "::/launchimages/loading.gif", gif_tmp],
                capture_output=True, text=True, timeout=10
            )
            gif_data = b""
            if r.returncode == 0 and os.path.isfile(gif_tmp):
                with open(gif_tmp, 'rb') as f:
                    gif_data = f.read()
            self.assets["launchimages/loading.gif"] = BootAsset(
                name="launchimages/loading.gif", display_name="Game Launch GIF", category="launchimages",
                width=0, height=0, bpp=0, size_bytes=len(gif_data),
                original_data=gif_data
            )

            # 5. Try to read launchimages/loading.jpg
            jpg_tmp = os.path.join(td, "loading.jpg")
            r = subprocess.run(
                ["mcopy", "-i", dev_str, "::/launchimages/loading.jpg", jpg_tmp],
                capture_output=True, text=True, timeout=10
            )
            jpg_data = b""
            if r.returncode == 0 and os.path.isfile(jpg_tmp):
                with open(jpg_tmp, 'rb') as f:
                    jpg_data = f.read()
            self.assets["launchimages/loading.jpg"] = BootAsset(
                name="launchimages/loading.jpg", display_name="Game Launch Image (JPG)", category="launchimages",
                width=640, height=480, bpp=24, size_bytes=len(jpg_data),
                original_data=jpg_data
            )

            # 6. Try to read launchimages/loading.mp4
            lmp4_tmp = os.path.join(td, "loading.mp4")
            r = subprocess.run(
                ["mcopy", "-i", dev_str, "::/launchimages/loading.mp4", lmp4_tmp],
                capture_output=True, text=True, timeout=10
            )
            lmp4_data = b""
            if r.returncode == 0 and os.path.isfile(lmp4_tmp):
                with open(lmp4_tmp, 'rb') as f:
                    lmp4_data = f.read()
            self.assets["launchimages/loading.mp4"] = BootAsset(
                name="launchimages/loading.mp4", display_name="Game Launch Video (MP4)", category="launchimages",
                width=0, height=0, bpp=0, size_bytes=len(lmp4_data),
                original_data=lmp4_data
            )

            # 7. Try to read launchimages/loading.ascii
            ascii_tmp = os.path.join(td, "loading.ascii")
            r = subprocess.run(
                ["mcopy", "-i", dev_str, "::/launchimages/loading.ascii", ascii_tmp],
                capture_output=True, text=True, timeout=10
            )
            ascii_data = b""
            if r.returncode == 0 and os.path.isfile(ascii_tmp):
                with open(ascii_tmp, 'rb') as f:
                    ascii_data = f.read()
            self.assets["launchimages/loading.ascii"] = BootAsset(
                name="launchimages/loading.ascii", display_name="Game Launch ASCII Art", category="launchimages",
                width=0, height=0, bpp=0, size_bytes=len(ascii_data),
                original_data=ascii_data
            )

            # 8. Dynamically scan themes for console posters and select sounds
            self._read_theme_assets(img_path)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def _read_theme_assets(self, img_path: str):
        dev_str = f"{img_path}@@1732268032"
        # Find first theme folder
        r = subprocess.run(["mdir", "-i", dev_str, "::/themes"], capture_output=True, text=True, timeout=10)
        theme_names = []
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "<DIR>" in line:
                    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d+:\d+)", line)
                    if m:
                        after = line[m.end():].strip()
                        name_parts = after.split()
                        if name_parts:
                            long_name = name_parts[-1] if len(name_parts) > 1 else name_parts[0]
                            if long_name not in (".", ".."):
                                theme_names.append(long_name)
        
        if not theme_names:
            return

        theme_name = theme_names[0]
        
        # Scan posters directory (just list names/sizes, do not download!)
        posters_dir = f"::/themes/{theme_name}/_art/posters"
        r = subprocess.run(["mdir", "-i", dev_str, posters_dir], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "<DIR>" not in line and any(ext in line.lower() for ext in (".jpg", ".png")):
                    # Match file size
                    m = re.search(r"(\d+)\s+\d{4}-\d{2}-\d{2}", line)
                    if m:
                        size_bytes = int(m.group(1))
                        parts = line[m.end():].strip().split()
                        if parts:
                            fname = parts[-1] if len(parts) > 1 else parts[0]
                            key = f"themes/{theme_name}/_art/posters/{fname}"
                            disp = f"Console Poster: {fname.split('.')[0].upper()}"
                            self.assets[key] = BootAsset(
                                name=key, display_name=disp, category="theme_posters",
                                width=0, height=0, bpp=0, size_bytes=size_bytes,
                                original_data=None # Lazy loaded
                            )



    def resize_easyroms(self, target_size_bytes: int) -> tuple[bool, str]:
        """
        Resize the EASYROMS partition (Partition 1) inside the loaded .img file or SD card.
        1. Truncate image file (if not a physical device).
        2. Update Partition 1 sector count in MBR.
        3. Format EASYROMS partition as FAT32.
        4. Recreate default game directories.
        """
        if not self.info:
            return False, "No target loaded."
        
        img_path = self.info.path
        is_device = img_path.startswith("/dev/")
        dev_str = f"{img_path}@@1732268032"
        
        # Get list of existing directories to restore
        r = subprocess.run(["mdir", "-i", dev_str, "::/"], capture_output=True, text=True, timeout=10)
        dirs_to_restore = []
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "<DIR>" in line:
                    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d+:\d+)", line)
                    if m:
                        before = line[:m.start()].rstrip()
                        after = line[m.end():].strip()
                        name = after if after else before.replace("<DIR>", "").strip()
                        name_parts = name.split()
                        if name_parts:
                            name = name_parts[0]
                        if name not in (".", "..", "System", "Information"):
                            dirs_to_restore.append(name.lower())
        
        if not dirs_to_restore:
            dirs_to_restore = [
                "arcade", "atari2600", "atari7800", "bezels", "bgm", "coleco",
                "dreamcast", "famicom", "gb", "gba", "gbc", "genesis", "mame",
                "mastersystem", "megadrive", "n64", "nds", "neogeo", "nes",
                "pcengine", "psx", "psp", "saturn", "screenshots", "segacd",
                "snes", "splash"
            ]

        try:
            write_path = img_path.replace("/dev/disk", "/dev/rdisk") if is_device else img_path
            
            # Update MBR and truncate image if file
            with open(write_path, "r+b") as f:
                if not is_device:
                    f.truncate(target_size_bytes)
                else:
                    # For physical devices, target size is the device size
                    target_size_bytes = self.info.size_bytes
                    
                f.seek(0)
                mbr = bytearray(f.read(512))
                
                # Partition 1 is at offset 446 (0x1BE)
                ptype = mbr[446 + 4]
                start_lba = struct.unpack_from("<I", mbr, 446 + 8)[0]
                
                total_sectors = target_size_bytes // 512
                new_sectors = total_sectors - start_lba
                
                # Pack new sector count
                struct.pack_into("<I", mbr, 446 + 12, new_sectors)
                
                f.seek(0)
                f.write(mbr)
            
            # Format partition 1
            if not is_device:
                did = self._attach(img_path)
                if not did:
                    return False, "hdiutil attach failed during resize."
                slice_dev = f"{did}s1"
                rf = subprocess.run(['newfs_msdos', '-F', '32', '-v', 'EASYROMS', slice_dev], capture_output=True, text=True, timeout=30)
                self._detach(did)
            else:
                slice_dev = f"{img_path}s1"
                # Unmount physical device first so we can format it
                subprocess.run(["diskutil", "unmount", slice_dev], capture_output=True)
                rf = subprocess.run(['newfs_msdos', '-F', '32', '-v', 'EASYROMS', slice_dev], capture_output=True, text=True, timeout=30)
                # Remount
                subprocess.run(["diskutil", "mount", slice_dev], capture_output=True)
            
            if rf.returncode != 0:
                return False, f"Format failed: {rf.stderr.strip() if rf.stderr else 'unknown error'}"
            
            # Recreate directories
            for d in dirs_to_restore:
                subprocess.run(["mmd", "-i", dev_str, f"::/{d}"], capture_output=True)
            
            if not is_device:
                self.info.size_bytes = target_size_bytes
                return True, f"Successfully resized game partition to {target_size_bytes // (1024**3)} GB"
            else:
                return True, "Successfully expanded game partition to full SD card size"
            
        except Exception as exc:
            return False, f"Resize failed: {exc}"

    def _ingest(self, path: str, name: str, display: str, cat: str):
        """Read one BMP, parse its header, store as BootAsset."""
        try:
            data = open(path, "rb").read()
            if len(data) < 54 or data[:2] != b"BM":
                return
            w = struct.unpack_from("<i", data, 18)[0]
            h = abs(struct.unpack_from("<i", data, 22)[0])
            bpp = struct.unpack_from("<H", data, 28)[0]
            self.assets[name] = BootAsset(
                name=name, display_name=display, category=cat,
                width=w, height=h, bpp=bpp, size_bytes=len(data),
                original_data=data,
            )
        except Exception:
            pass

    # ── low-level subprocess wrappers ────────────────────────────

    @staticmethod
    def _attach(img: str) -> Optional[str]:
        try:
            r = subprocess.run(
                ["hdiutil", "attach", "-nomount", img],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                for ln in r.stdout.strip().splitlines():
                    tok = ln.strip().split()
                    if tok and tok[0].startswith("/dev/disk"):
                        return tok[0]
        except Exception:
            pass
        return None

    @staticmethod
    def _detach(did: str):
        try:
            subprocess.run(
                ["hdiutil", "detach", did, "-force"],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass

    @staticmethod
    def _mnt(did: str, point: str) -> bool:
        try:
            r = subprocess.run(
                ["mount", "-t", "msdos", did, point],
                capture_output=True, text=True, timeout=15,
            )
            return r.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _umnt(point: str):
        try:
            subprocess.run(["umount", point], capture_output=True, timeout=15)
        except Exception:
            pass

    # ── image conversion (Pillow) ────────────────────────────────

    @staticmethod
    def _convert_bmp(src: str, tw: int, th: int) -> bytes:
        """Open any image, resize/crop to *tw*×*th*, export as 24-bit BMP."""
        img = PILImage.open(src).convert("RGB")

        if img.size != (tw, th):
            # resize to cover, then centre-crop
            sr = img.width / img.height
            tr = tw / th
            if sr > tr:
                nw, nh = int(th * sr), th
            else:
                nw, nh = tw, int(tw / sr)
            img = img.resize((max(nw, 1), max(nh, 1)), PILImage.LANCZOS)
            left = (img.width - tw) // 2
            top = (img.height - th) // 2
            img = img.crop((left, top, left + tw, top + th))

        buf = io.BytesIO()
        img.save(buf, format="BMP")
        return buf.getvalue()
