"""
R36S SD Card Manager — Desktop application.

A native macOS file manager for R36S handheld game console SD cards.
Reads/writes to hidden FAT32 partitions via mtools, bypassing macOS
mount restrictions.
"""

import sys
import os
import io
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QToolBar, QStatusBar, QFileDialog,
    QMessageBox, QHeaderView, QAbstractItemView, QLabel, QWidget,
    QVBoxLayout, QHBoxLayout, QSizePolicy, QLineEdit, QToolButton,
    QStackedWidget, QListWidget, QListWidgetItem, QGroupBox, QPushButton,
    QTabWidget, QDialog, QSlider, QFormLayout, QDialogButtonBox,
    QTextEdit,
)
from PySide6.QtCore import Qt, QTimer, QMimeData, Signal, QThread
from PySide6.QtGui import (
    QAction, QIcon, QFont, QDragEnterEvent, QDropEvent,
    QKeySequence, QPalette, QColor, QImage, QPixmap,
)

from r36s_device import R36SDevice, DeviceInfo, FileEntry, ROM_FILTERS, format_size
from image_editor import BootPartitionEditor
from PIL import Image as PILImage
import tempfile
import uuid


# ======================================================================
# Drop-aware table
# ======================================================================

class FileTable(QTableWidget):
    """QTableWidget that accepts file drops from Finder."""

    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)


# ======================================================================
# Main Window
# ======================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.device = R36SDevice()
        self.current_path = "/"
        self._free_bytes = 0
        self.history = []
        self.history_index = -1
        self.box_art_loader = None

        self._setup_window()
        self._setup_toolbar()
        self._setup_central()
        self._setup_statusbar()
        self._update_ui_state()

        # Kick off device scan after the window appears
        QTimer.singleShot(200, self._initial_scan)

    # ------------------------------------------------------------------
    # UI Setup
    # ------------------------------------------------------------------

    def _setup_window(self):
        self.setWindowTitle("WorkaholicSEA — R36S Manager")
        self.resize(1000, 660)
        self.setMinimumSize(750, 480)

    def _setup_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setIconSize(tb.iconSize())
        self.addToolBar(Qt.TopToolBarArea, tb)

        icon = self.style()

        # Scan / Refresh
        self.act_refresh = QAction(
            icon.standardIcon(icon.StandardPixmap.SP_BrowserReload),
            "Refresh", self,
        )
        self.act_refresh.setShortcut(QKeySequence.Refresh)
        self.act_refresh.triggered.connect(self._on_refresh)
        tb.addAction(self.act_refresh)

        # Add Game
        self.act_add = QAction(
            icon.standardIcon(icon.StandardPixmap.SP_FileDialogNewFolder),
            "Add Game", self,
        )
        self.act_add.setShortcut(QKeySequence("Ctrl+O"))
        self.act_add.triggered.connect(self._on_add_game)
        tb.addAction(self.act_add)

        # Delete
        self.act_delete = QAction(
            icon.standardIcon(icon.StandardPixmap.SP_TrashIcon),
            "Delete", self,
        )
        self.act_delete.setShortcut(QKeySequence.Delete)
        self.act_delete.triggered.connect(self._on_delete)
        tb.addAction(self.act_delete)

        tb.addSeparator()

        # Eject
        self.act_eject = QAction(
            icon.standardIcon(icon.StandardPixmap.SP_DialogCloseButton),
            "Eject", self,
        )
        self.act_eject.triggered.connect(self._on_eject)
        tb.addAction(self.act_eject)

        tb.addSeparator()

        # Edit Backup Image
        self.act_edit_image = QAction(
            icon.standardIcon(icon.StandardPixmap.SP_FileDialogStart),
            "Edit Image Logo", self,
        )
        self.act_edit_image.triggered.connect(self._on_open_backup_image)
        tb.addAction(self.act_edit_image)

        # Spacer → push device label to the right
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        # Device label
        self.device_label = QLabel("  No device  ")
        self.device_label.setStyleSheet("color: #999; padding: 0 12px; font-size: 12px;")
        tb.addWidget(self.device_label)

    def _setup_central(self):
        splitter = QSplitter(Qt.Horizontal)

        # --- Left sidebar: search + directory tree ---
        sidebar = QWidget()
        sidebar.setObjectName("sidebarContainer")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        # Search field
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search consoles…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setObjectName("sidebarSearch")
        self.search_input.textChanged.connect(self._on_search_changed)
        sidebar_layout.addWidget(self.search_input)

        # Directory tree
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(18)
        self.tree.itemClicked.connect(self._on_tree_clicked)
        self.tree.itemExpanded.connect(self._on_tree_expanded)
        self.tree.setObjectName("sidebar")
        sidebar_layout.addWidget(self.tree)

        sidebar.setMinimumWidth(200)

        # --- Right: Navigation + File table ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Breadcrumbs bar
        nav_bar = QWidget()
        nav_bar.setObjectName("breadcrumbBar")
        nav_layout = QHBoxLayout(nav_bar)
        nav_layout.setContentsMargins(8, 4, 8, 4)
        nav_layout.setSpacing(4)

        icon = self.style()

        self.btn_back = QToolButton()
        self.btn_back.setIcon(icon.standardIcon(icon.StandardPixmap.SP_ArrowLeft))
        self.btn_back.setToolTip("Back")
        self.btn_back.clicked.connect(self._on_back)
        nav_layout.addWidget(self.btn_back)

        self.btn_forward = QToolButton()
        self.btn_forward.setIcon(icon.standardIcon(icon.StandardPixmap.SP_ArrowRight))
        self.btn_forward.setToolTip("Forward")
        self.btn_forward.clicked.connect(self._on_forward)
        nav_layout.addWidget(self.btn_forward)

        self.btn_up = QToolButton()
        self.btn_up.setIcon(icon.standardIcon(icon.StandardPixmap.SP_ArrowUp))
        self.btn_up.setToolTip("Up")
        self.btn_up.clicked.connect(self._on_up)
        nav_layout.addWidget(self.btn_up)

        self.lbl_breadcrumb = QLabel("/")
        self.lbl_breadcrumb.setObjectName("breadcrumbText")
        nav_layout.addWidget(self.lbl_breadcrumb)
        nav_layout.addStretch()

        right_layout.addWidget(nav_bar)

        self.table = FileTable()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Name", "Size", "Modified"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.setShowGrid(False)
        self.table.files_dropped.connect(self._on_files_dropped)
        self.table.doubleClicked.connect(self._on_table_double_click)
        self.table.itemSelectionChanged.connect(self._on_game_selected)

        right_layout.addWidget(self.table)

        # Detail Panel (Far Right)
        self.detail_panel = QGroupBox("Game Info / Box Art")
        self.detail_layout = QVBoxLayout(self.detail_panel)
        self.detail_layout.setSpacing(12)
        
        self.lbl_game_title = QLabel("Select a game to view details.")
        self.lbl_game_title.setWordWrap(True)
        self.lbl_game_title.setStyleSheet("font-weight: bold; font-size: 13px; color: #24292f;")
        self.detail_layout.addWidget(self.lbl_game_title)
        
        self.lbl_game_art = QLabel()
        self.lbl_game_art.setAlignment(Qt.AlignCenter)
        self.lbl_game_art.setFixedSize(200, 240)
        self.lbl_game_art.setStyleSheet("border: 1px dashed #d1d9e0; background-color: #f6f8fa; border-radius: 6px;")
        self.lbl_game_art.setText("No Preview")
        self.detail_layout.addWidget(self.lbl_game_art)
        
        self.btn_change_art = QPushButton("Change Box Art...")
        self.btn_change_art.clicked.connect(self._on_change_game_art)
        self.btn_change_art.setEnabled(False)
        self.detail_layout.addWidget(self.btn_change_art)
        
        self.btn_delete_art = QPushButton("Delete Box Art")
        self.btn_delete_art.clicked.connect(self._on_delete_game_art)
        self.btn_delete_art.setEnabled(False)
        self.btn_delete_art.setStyleSheet("color: #cf222e;")
        self.detail_layout.addWidget(self.btn_delete_art)
        
        self.detail_layout.addStretch()

        splitter.addWidget(sidebar)
        splitter.addWidget(right_panel)
        splitter.addWidget(self.detail_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([200, 580, 220])

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.addTab(splitter, "Game Manager")

        self.editor_widget = ImageEditorWidget()
        self.editor_widget.back_requested.connect(lambda: self.tabs.setCurrentIndex(0))
        self.tabs.addTab(self.editor_widget, "Boot Asset Editor")

        self.tabs.currentChanged.connect(lambda index: self._update_ui_state())

        self.setCentralWidget(self.tabs)

    def _setup_statusbar(self):
        self.status_label = QLabel("Ready")
        self.statusBar().addWidget(self.status_label, 1)
        self.free_label = QLabel("")
        self.statusBar().addPermanentWidget(self.free_label)

    # ------------------------------------------------------------------
    # UI state
    # ------------------------------------------------------------------

    def _update_ui_state(self):
        connected = self.device.connected
        in_editor = (hasattr(self, 'tabs') and self.tabs.currentIndex() == 1)

        self.act_refresh.setEnabled(True)
        self.act_add.setEnabled(connected and not in_editor)
        self.act_delete.setEnabled(connected and not in_editor)
        
        is_img = connected and self.device.device.label == "Disk Image"
        self.act_eject.setEnabled(connected and not is_img and not in_editor)
        
        self.btn_back.setEnabled(self.history_index > 0 and not in_editor)
        self.btn_forward.setEnabled(self.history_index < len(self.history) - 1 and not in_editor)
        self.btn_up.setEnabled(self.current_path != "/" and connected and not in_editor)

        if hasattr(self, 'tabs'):
            self.tabs.setTabEnabled(0, connected)
            self.tabs.setTabEnabled(1, connected)

        if not connected:
            self.device_label.setText("  No device  ")
            self.device_label.setStyleSheet("color: #999; padding: 0 12px; font-size: 12px;")
            self.free_label.setText("")
            self.lbl_breadcrumb.setText(" Not Connected ")
        else:
            dev = self.device.device
            lbl = dev.device_path.split("/")[-1]
            if dev.label == "Disk Image":
                self.device_label.setText(f"  ● Image File ({format_size(dev.size_bytes)})  ")
                self.device_label.setStyleSheet("color: #0969da; font-weight: 600; padding: 0 12px; font-size: 12px;")
            else:
                self.device_label.setText(f"  ● {lbl}  ({format_size(dev.size_bytes)})  ")
                self.device_label.setStyleSheet("color: #1a7f37; font-weight: 600; padding: 0 12px; font-size: 12px;")
                
            if self._free_bytes:
                self.free_label.setText(f"  {format_size(self._free_bytes)} free  ")

            # Update breadcrumb label
            parts = self.current_path.strip("/").split("/")
            if not parts or parts == [""]:
                self.lbl_breadcrumb.setText(" R36S SD Card " if dev.label != "Disk Image" else f" Image: {os.path.basename(dev.device_path)} ")
            else:
                self.lbl_breadcrumb.setText(" › ".join(["R36S"] + parts))

    def _set_status(self, msg: str):
        self.status_label.setText(msg)
        QApplication.processEvents()

    # ------------------------------------------------------------------
    # Device scan / connect
    # ------------------------------------------------------------------

    def _initial_scan(self):
        # 1. Scan for physical disks
        disks = self.device.scan_disks()
        if disks:
            # Connect to physical disk
            self._on_refresh()
            return
            
        # 2. If no physical disk, check for backup_r36s_clone.img
        default_img = "/Users/huudung/Desktop/r36s/backup_r36s_clone.img"
        if os.path.isfile(default_img):
            self._set_status("No SD card detected. Loading backup_r36s_clone.img...")
            ok, msg = self.device.connect_image(default_img)
            if ok:
                self._load_root()
                # Load boot assets in editor
                self.editor_widget.load_image_file(default_img)
                self._update_ui_state()
                self._set_status(f"Loaded {os.path.basename(default_img)}")
                return
                
        # 3. Fallback: prompt no device
        self._on_refresh()

    def _on_refresh(self):
        """Scan for devices, connect, load tree."""
        self._set_status("Scanning for SD cards…")
        QApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            disks = self.device.scan_disks()
        finally:
            QApplication.restoreOverrideCursor()

        if not disks:
            self._set_status("No external SD card detected.")
            QMessageBox.information(
                self, "No Device",
                "No external SD card found.\n\n"
                "Please insert your R36S SD card and click Refresh.",
            )
            self._update_ui_state()
            return

        # Pick the first external disk (most users have only one)
        target = disks[0]

        self._set_status(f"Connecting to {target.device_path}…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, msg = self.device.connect(target)
        finally:
            QApplication.restoreOverrideCursor()

        if not ok:
            self._set_status("Connection failed.")
            QMessageBox.warning(self, "Connection Failed", msg)
            self._update_ui_state()
            return

        self._set_status("Loading directories…")
        self._load_root()
        # Load boot assets in the editor widget
        self.editor_widget.load_image_file(target.device_path)
        self._update_ui_state()
        self._set_status("Ready")

    # ------------------------------------------------------------------
    # Tree operations
    # ------------------------------------------------------------------

    def _load_root(self):
        """Populate the tree with root-level directories."""
        self.history = []
        self.history_index = -1
        self.tree.clear()
        self.table.setRowCount(0)

        entries, free = self.device.list_dir("/")
        self._free_bytes = free

        # Seed root to history
        self.history.append("/")
        self.history_index = 0

        for entry in entries:
            if entry.is_dir:
                if entry.name.lower() not in ROM_FILTERS:
                    continue
                item = QTreeWidgetItem([entry.name])
                item.setData(0, Qt.UserRole, entry.name)
                # Add a dummy child so the expand arrow shows
                item.addChild(QTreeWidgetItem(["…"]))
                self.tree.addTopLevelItem(item)

        self._update_ui_state()

    def _on_search_changed(self, text: str):
        """Filter the sidebar tree by search text."""
        query = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if not query:
                item.setHidden(False)
            else:
                name = (item.data(0, Qt.UserRole) or item.text(0)).lower()
                item.setHidden(query not in name)

    def _on_tree_clicked(self, item: QTreeWidgetItem, _col: int):
        path = self._tree_item_path(item)
        self._load_directory(path)

    def _on_tree_expanded(self, item: QTreeWidgetItem):
        """Lazy-load subdirectories when a tree node is expanded."""
        # Remove dummy child
        if item.childCount() == 1 and item.child(0).text(0) == "…":
            item.removeChild(item.child(0))

            path = self._tree_item_path(item)
            entries, _ = self.device.list_dir(path)
            for e in entries:
                if e.is_dir:
                    child = QTreeWidgetItem([e.name])
                    child.setData(0, Qt.UserRole, e.name)
                    child.addChild(QTreeWidgetItem(["…"]))
                    item.addChild(child)

    @staticmethod
    def _tree_item_path(item: QTreeWidgetItem) -> str:
        """Build the full path by walking up the tree."""
        parts = []
        node = item
        while node:
            parts.append(node.data(0, Qt.UserRole) or node.text(0))
            node = node.parent()
        parts.reverse()
        return "/".join(parts)

    # ------------------------------------------------------------------
    # Table operations
    # ------------------------------------------------------------------

    def _on_back(self):
        if self.history_index > 0:
            self.history_index -= 1
            self._load_directory(self.history[self.history_index], push_history=False)

    def _on_forward(self):
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self._load_directory(self.history[self.history_index], push_history=False)

    def _on_up(self):
        if self.current_path != "/":
            parts = self.current_path.strip("/").split("/")
            parent = "/" + "/".join(parts[:-1])
            if parent == "/":
                parent = "/"
            self._load_directory(parent)

    def _load_directory(self, path: str, push_history=True):
        """Show files for *path* in the table."""
        self._clear_game_detail()
        if push_history:
            self.history = self.history[:self.history_index + 1]
            if not self.history or self.history[-1] != path:
                self.history.append(path)
                self.history_index += 1

        self.current_path = path
        self._set_status(f"Loading /{path}…")
        QApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            entries, free = self.device.list_dir(path)
        finally:
            QApplication.restoreOverrideCursor()

        self._free_bytes = free
        self._update_ui_state()

        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)

        for entry in entries:
            if path == "/" and entry.is_dir and entry.name.lower() not in ROM_FILTERS:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)

            # Name
            name_item = QTableWidgetItem(entry.name)
            name_item.setData(Qt.UserRole, entry)
            if entry.is_dir:
                name_item.setText(f"📁  {entry.name}")
            self.table.setItem(row, 0, name_item)

            # Size
            size_text = "—" if entry.is_dir else format_size(entry.size)
            size_item = QTableWidgetItem(size_text)
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if not entry.is_dir:
                size_item.setData(Qt.UserRole + 1, entry.size)
            self.table.setItem(row, 1, size_item)

            # Date
            date_text = f"{entry.date}  {entry.time}" if entry.date else ""
            self.table.setItem(row, 2, QTableWidgetItem(date_text))

        self.table.setSortingEnabled(True)
        self._set_status(f"/{path}  —  {len(entries)} items")

    def _on_table_double_click(self, index):
        """Navigate into a subdirectory on double-click."""
        row = index.row()
        name_item = self.table.item(row, 0)
        if not name_item:
            return
        entry: FileEntry = name_item.data(Qt.UserRole)
        if entry and entry.is_dir:
            new_path = f"{self.current_path.rstrip('/')}/{entry.name}"
            self._load_directory(new_path)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_add_game(self):
        """Open file picker and copy selected files to current directory."""
        if not self.device.connected:
            return

        # Build filter based on current directory name
        dir_name = self.current_path.rstrip("/").split("/")[-1].lower()
        rom_filter = ROM_FILTERS.get(dir_name, "")
        all_filter = "All Files (*)"
        if rom_filter:
            combined = f"{rom_filter};;{all_filter}"
        else:
            combined = all_filter

        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select ROM files", "", combined,
        )
        if not paths:
            return

        self._copy_files(paths)

    def _on_files_dropped(self, paths: list[str]):
        """Handle files dropped onto the table from Finder."""
        if not self.device.connected:
            return
        # Filter out directories (only copy files)
        file_paths = [p for p in paths if os.path.isfile(p)]
        if file_paths:
            self._copy_files(file_paths)

    def _copy_files(self, local_paths: list[str]):
        """Copy files into the currently selected directory."""
        names = [os.path.basename(p) for p in local_paths]
        target = f"/{self.current_path.strip('/')}"

        self._set_status(f"Copying {len(local_paths)} file(s) to {target}…")
        QApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            ok, msg = self.device.copy_to(local_paths, self.current_path)
        finally:
            QApplication.restoreOverrideCursor()

        if ok:
            self._set_status(f"Copied: {', '.join(names)}")
            self._load_directory(self.current_path)
        else:
            self._set_status("Copy failed")
            QMessageBox.warning(self, "Copy Failed", msg)

    def _on_delete(self):
        """Delete the selected file(s) in the table."""
        if not self.device.connected:
            return

        selected = self.table.selectedItems()
        if not selected:
            return

        # Collect unique rows
        rows = sorted({item.row() for item in selected})
        entries_to_delete: list[FileEntry] = []
        for r in rows:
            name_item = self.table.item(r, 0)
            if name_item:
                entry = name_item.data(Qt.UserRole)
                if entry:
                    entries_to_delete.append(entry)

        if not entries_to_delete:
            return

        names = [e.name for e in entries_to_delete]
        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Delete {len(names)} item(s)?\n\n" + "\n".join(names[:10]),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._set_status("Deleting…")
        QApplication.setOverrideCursor(Qt.WaitCursor)

        errors = []
        for entry in entries_to_delete:
            full_path = f"{self.current_path.rstrip('/')}/{entry.name}"
            if entry.is_dir:
                ok, msg = self.device.delete_dir(full_path)
            else:
                ok, msg = self.device.delete_file(full_path)
            if not ok:
                errors.append(f"{entry.name}: {msg}")

        QApplication.restoreOverrideCursor()

        if errors:
            QMessageBox.warning(
                self, "Delete Errors", "\n".join(errors)
            )

        self._load_directory(self.current_path)
        self._set_status("Done")

    def _on_eject(self):
        """Eject the SD card."""
        if not self.device.connected:
            return

        reply = QMessageBox.question(
            self, "Eject",
            "Eject the SD card safely?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        ok, msg = self.device.eject()
        self.history = []
        self.history_index = -1
        self.tree.clear()
        self.table.setRowCount(0)
        self._update_ui_state()

        if ok:
            self._set_status("SD card ejected. You can remove it now.")
            QMessageBox.information(
                self, "Ejected",
                "SD card ejected safely.\nYou can remove it now.",
            )
        else:
            self._set_status("Eject failed")
            QMessageBox.warning(self, "Eject Failed", msg)

    def _on_open_backup_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open R36S Backup Image", "", "Disk Images (*.img)"
        )
        if not file_path:
            return
        
        self._set_status("Opening image...")
        ok, msg = self.device.connect_image(file_path)

        if not ok:
            self._set_status("Failed to load image.")
            QMessageBox.critical(self, "Load Failed", f"Could not load image file:\n{msg}")
            return

        self._load_root()
        self._update_ui_state()
        self._set_status(f"Loading assets from {os.path.basename(file_path)}...")
        # Load assets asynchronously
        self.editor_widget.load_image_file(file_path)

    # ------------------------------------------------------------------
    # Game Detail / Box Art Operations
    # ------------------------------------------------------------------

    def _on_game_selected(self):
        selected = self.table.selectedItems()
        if not selected:
            self._clear_game_detail()
            return
        
        row = selected[0].row()
        name_item = self.table.item(row, 0)
        entry = name_item.data(Qt.UserRole)
        if not entry or entry.is_dir:
            self._clear_game_detail()
            return
            
        self._load_game_detail(entry)

    def _clear_game_detail(self):
        self.selected_game_entry = None
        self.lbl_game_title.setText("Select a game to view details.")
        self.lbl_game_art.clear()
        self.lbl_game_art.setText("No Preview")
        self.btn_change_art.setEnabled(False)
        self.btn_delete_art.setEnabled(False)

    def _load_game_detail(self, entry):
        if self.box_art_loader and self.box_art_loader.isRunning():
            self.box_art_loader.cancel()
            self.box_art_loader.wait()
            
        self.selected_game_entry = entry
        self.lbl_game_title.setText(entry.name)
        self.btn_change_art.setEnabled(True)
        self.btn_delete_art.setEnabled(False)
        
        self.lbl_game_art.clear()
        self.lbl_game_art.setText("Loading Box Art...")
        
        # Start loader thread
        self.box_art_loader = BoxArtLoader(
            self.device,
            self.current_path,
            entry.name,
            self.lbl_game_art.width(),
            self.lbl_game_art.height()
        )
        self.box_art_loader.loaded.connect(self._on_box_art_loaded)
        self.box_art_loader.start()

    def _on_box_art_loaded(self, found_path, pm):
        if pm.isNull():
            self.lbl_game_art.clear()
            self.lbl_game_art.setText("No Box Art")
            self.selected_game_art_path = None
            self.btn_delete_art.setEnabled(False)
        else:
            self.lbl_game_art.setPixmap(pm)
            self.selected_game_art_path = found_path
            self.btn_delete_art.setEnabled(True)

    def _on_change_game_art(self):
        if not hasattr(self, 'selected_game_entry') or not self.selected_game_entry:
            return
            
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Box Art Image", "", "Images (*.png *.jpg *.jpeg)"
        )
        if not file_path:
            return
            
        entry = self.selected_game_entry
        base, _ = os.path.splitext(entry.name)
        
        # If we have an existing path, use it. Otherwise determine appropriate target path
        if hasattr(self, 'selected_game_art_path') and self.selected_game_art_path:
            remote_art_path = self.selected_game_art_path
        else:
            test_dir = f"/{self.current_path.strip('/')}/downloaded_images"
            if self.device.file_exists(test_dir):
                remote_art_path = f"{test_dir}/{base}.png"
            else:
                remote_art_path = f"/{self.current_path.strip('/')}/images/{base}.png"
        
        # Let users crop the box art to 3:4 ratio standard vertically
        crop_dialog = ImageCropDialog(file_path, 300, 400, self)
        crop_dialog.setWindowTitle("Adjust Box Art Crop")
        if crop_dialog.exec() == QDialog.Accepted:
            cropped_img = crop_dialog.get_cropped_image()
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                temp_path = tf.name
            try:
                cropped_img.save(temp_path, format="PNG")
                if self.device.upload_file(temp_path, remote_art_path):
                    self._load_game_detail(entry)
                else:
                    QMessageBox.warning(self, "Upload Failed", "Could not upload box art image to SD card/image.")
            finally:
                try:
                    os.remove(temp_path)
                except:
                    pass

    def _on_delete_game_art(self):
        if not hasattr(self, 'selected_game_art_path') or not self.selected_game_art_path:
            return
            
        reply = QMessageBox.question(
            self, "Delete Box Art",
            f"Are you sure you want to delete box art for {self.selected_game_entry.name}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            if self.device.delete_file(self.selected_game_art_path):
                self._load_game_detail(self.selected_game_entry)
            else:
                QMessageBox.warning(self, "Delete Failed", "Could not delete box art file.")


# ======================================================================
# Image Crop Dialog
# ======================================================================

class ImageCropDialog(QDialog):
    def __init__(self, src_path, tw, th, parent=None):
        super().__init__(parent)
        print(f"[DEBUG] ImageCropDialog init: src_path={src_path}, tw={tw}, th={th}")
        self.setWindowTitle("Adjust Crop & Scale")
        self.setMinimumWidth(500)
        self.setModal(True)

        self.src_path = src_path
        self.tw = tw
        self.th = th
        
        # Load image
        self.pil_img = PILImage.open(src_path).convert("RGB")
        self.sw, self.sh = self.pil_img.size
        self.tr = tw / th
        
        # Calculate max crop size
        if self.sw / self.sh > self.tr:
            self.max_ch = self.sh
            self.max_cw = int(self.sh * self.tr)
        else:
            self.max_cw = self.sw
            self.max_ch = int(self.sw / self.tr)

        self._setup_ui()
        self._update_preview()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Title / Info
        info = QLabel("Drag sliders to zoom and position the image inside the crop area:")
        info.setStyleSheet("font-weight: bold;")
        layout.addWidget(info)

        # Preview area (dimmed background)
        self.lbl_preview = QLabel()
        self.lbl_preview.setAlignment(Qt.AlignCenter)
        self.lbl_preview.setFixedSize(360, 270) # 4:3 preview ratio
        self.lbl_preview.setStyleSheet("border: 2px solid #0969da; background-color: #24292f; border-radius: 6px;")
        
        preview_container = QHBoxLayout()
        preview_container.addStretch()
        preview_container.addWidget(self.lbl_preview)
        preview_container.addStretch()
        layout.addLayout(preview_container)

        # Sliders Form
        form = QFormLayout()
        form.setSpacing(8)

        # Zoom
        self.sld_zoom = QSlider(Qt.Horizontal)
        self.sld_zoom.setRange(100, 400) # 1.0x to 4.0x
        self.sld_zoom.setValue(100)
        self.sld_zoom.valueChanged.connect(self._update_preview)
        form.addRow("Zoom / Scale:", self.sld_zoom)

        # X Pan
        self.sld_x = QSlider(Qt.Horizontal)
        self.sld_x.setRange(0, 100)
        self.sld_x.setValue(50)
        self.sld_x.valueChanged.connect(self._update_preview)
        form.addRow("Horizontal position (X):", self.sld_x)

        # Y Pan
        self.sld_y = QSlider(Qt.Horizontal)
        self.sld_y.setRange(0, 100)
        self.sld_y.setValue(50)
        self.sld_y.valueChanged.connect(self._update_preview)
        form.addRow("Vertical position (Y):", self.sld_y)

        layout.addLayout(form)

        # Button Box
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _update_preview(self):
        z = self.sld_zoom.value() / 100.0
        px = self.sld_x.value()
        py = self.sld_y.value()

        # Compute crop box dimensions
        cw = self.max_cw / z
        ch = self.max_ch / z

        # Compute available movement space
        max_x = self.sw - cw
        max_y = self.sh - ch

        # Compute offsets
        x_off = max_x * px / 100.0
        y_off = max_y * py / 100.0

        # Perform crop & resize
        print(f"[DEBUG] _update_preview: cropping at x={x_off}, y={y_off}, w={cw}, h={ch}")
        cropped = self.pil_img.crop((x_off, y_off, x_off + cw, y_off + ch))
        resized = cropped.resize((self.tw, self.th), PILImage.LANCZOS)

        # Convert resized PIL to QImage (safely using in-memory PNG to prevent GC crashes)
        print(f"[DEBUG] _update_preview: converting to PNG in memory")
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        qimg = QImage.fromData(buf.getvalue())
        
        # Scale to fit preview box keeping aspect ratio
        pm = QPixmap.fromImage(qimg)
        pm_scaled = pm.scaled(self.lbl_preview.width() - 4, self.lbl_preview.height() - 4, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.lbl_preview.setPixmap(pm_scaled)
        print(f"[DEBUG] _update_preview: preview updated successfully")
        
        # Save current cropped state
        self.cropped_img = resized

    def get_cropped_image(self):
        return self.cropped_img


# ======================================================================
# Image Editor Widget
# ======================================================================

class ImageEditorWidget(QWidget):
    back_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.editor = BootPartitionEditor()
        self.current_asset_name = None
        self._setup_ui()

    def _setup_ui(self):
        # Layout of ImageEditorWidget
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(12)

        # Top Info Bar
        info_bar = QWidget()
        info_bar.setObjectName("editorInfoBar")
        info_layout = QHBoxLayout(info_bar)
        info_layout.setContentsMargins(12, 8, 12, 8)
        
        self.lbl_image_path = QLabel("Image: None")
        self.lbl_image_path.setStyleSheet("font-weight: bold; font-size: 13px; color: #24292f;")
        info_layout.addWidget(self.lbl_image_path)
        
        self.lbl_fw_name = QLabel("Firmware: None")
        self.lbl_fw_name.setStyleSheet("color: #57606a; font-size: 13px;")
        info_layout.addWidget(self.lbl_fw_name)
        
        info_layout.addStretch()

        self.btn_resize = QPushButton("Resize EASYROMS Partition...")
        self.btn_resize.clicked.connect(self._on_resize_clicked)
        info_layout.addWidget(self.btn_resize)

        self.btn_back = QPushButton("Back to SD Manager")
        self.btn_back.clicked.connect(self.back_requested.emit)
        info_layout.addWidget(self.btn_back)

        main_layout.addWidget(info_bar)

        # Central Area (Splitter or HBox)
        content_splitter = QSplitter(Qt.Horizontal)

        # Left: Asset list
        self.asset_list = QListWidget()
        self.asset_list.setObjectName("assetList")
        self.asset_list.currentItemChanged.connect(self._on_asset_changed)
        content_splitter.addWidget(self.asset_list)

        # Right: Detail & Preview
        preview_container = QWidget()
        preview_layout = QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(12)

        # Grid or HBox for side-by-side preview
        previews_box = QHBoxLayout()
        
        # Original Preview
        orig_group = QGroupBox("Original / Default")
        orig_layout = QVBoxLayout(orig_group)
        self.lbl_orig_preview = QLabel("No Image")
        self.lbl_orig_preview.setAlignment(Qt.AlignCenter)
        self.lbl_orig_preview.setStyleSheet("border: 1px solid #d1d9e0; background-color: #f6f8fa; min-height: 240px; border-radius: 6px;")
        orig_layout.addWidget(self.lbl_orig_preview)
        previews_box.addWidget(orig_group)

        # Modified Preview
        mod_group = QGroupBox("Current / Modified")
        mod_layout = QVBoxLayout(mod_group)
        self.lbl_mod_preview = QLabel("No Image")
        self.lbl_mod_preview.setAlignment(Qt.AlignCenter)
        self.lbl_mod_preview.setStyleSheet("border: 1px solid #d1d9e0; background-color: #f6f8fa; min-height: 240px; border-radius: 6px;")
        mod_layout.addWidget(self.lbl_mod_preview)
        previews_box.addWidget(mod_group)

        # Stacked widget for switching between Image Preview and Text Editor
        self.right_stack = QStackedWidget()
        
        # Page 0: Image Previews
        self.img_preview_widget = QWidget()
        img_preview_layout = QVBoxLayout(self.img_preview_widget)
        img_preview_layout.setContentsMargins(0, 0, 0, 0)
        img_preview_layout.addLayout(previews_box)
        self.right_stack.addWidget(self.img_preview_widget)
        
        # Page 1: Text Editor
        self.txt_editor_widget = QWidget()
        txt_editor_layout = QVBoxLayout(self.txt_editor_widget)
        txt_editor_layout.setContentsMargins(0, 0, 0, 0)
        
        txt_group = QGroupBox("ASCII Text Editor")
        txt_group_layout = QVBoxLayout(txt_group)
        self.txt_editor = QTextEdit()
        self.txt_editor.setFont(QFont("Courier New", 10))
        self.txt_editor.setLineWrapMode(QTextEdit.NoWrap)
        self.txt_editor.textChanged.connect(self._on_text_edited)
        txt_group_layout.addWidget(self.txt_editor)
        txt_editor_layout.addWidget(txt_group)
        self.right_stack.addWidget(self.txt_editor_widget)

        preview_layout.addWidget(self.right_stack)

        # Asset Specs / Controls
        ctrl_bar = QHBoxLayout()
        self.lbl_specs = QLabel("Specs: None")
        self.lbl_specs.setStyleSheet("font-size: 12px; color: #57606a;")
        ctrl_bar.addWidget(self.lbl_specs)
        ctrl_bar.addStretch()

        self.btn_import = QPushButton("Import Custom Image...")
        self.btn_import.clicked.connect(self._on_import_clicked)
        ctrl_bar.addWidget(self.btn_import)

        self.btn_reset_asset = QPushButton("Reset to Default")
        self.btn_reset_asset.clicked.connect(self._on_reset_asset_clicked)
        ctrl_bar.addWidget(self.btn_reset_asset)

        preview_layout.addLayout(ctrl_bar)
        
        # Add Stretch at the bottom of preview
        preview_layout.addStretch()

        right_widget = QWidget()
        right_widget.setLayout(preview_layout)
        content_splitter.addWidget(right_widget)

        content_splitter.setSizes([250, 750])
        main_layout.addWidget(content_splitter)

        # Bottom Bar: Status of changes + Save
        bottom_bar = QWidget()
        bottom_bar.setObjectName("editorBottomBar")
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(8, 8, 8, 8)

        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet("font-size: 12px; color: #57606a;")
        bottom_layout.addWidget(self.lbl_status)
        bottom_layout.addStretch()

        self.btn_reset_all = QPushButton("Reset All Changes")
        self.btn_reset_all.clicked.connect(self._on_reset_all_clicked)
        bottom_layout.addWidget(self.btn_reset_all)

        self.btn_save = QPushButton("Save Changes to Image")
        self.btn_save.setObjectName("btnSaveImage")
        self.btn_save.clicked.connect(self._on_save_clicked)
        bottom_layout.addWidget(self.btn_save)

        main_layout.addWidget(bottom_bar)

    def load_image_file(self, file_path):
        """Start loading an image file in the background to avoid blocking the UI."""
        self.lbl_image_path.setText(f"Loading {os.path.basename(file_path)}...")
        self.lbl_fw_name.setText("Firmware: loading...")
        self.lbl_status.setText("Loading image assets... Please wait.")
        self.asset_list.clear()
        self.btn_import.setEnabled(False)
        self.btn_save.setEnabled(False)
        QApplication.processEvents()
        
        self._load_worker = ImageLoadWorker(self.editor, file_path)
        self._load_worker.finished_signal.connect(self._on_image_loaded)
        self._load_worker.start()

    def _on_image_loaded(self, ok, msg, file_path):
        if not ok:
            self.lbl_image_path.setText("No image loaded")
            self.lbl_fw_name.setText("Firmware: N/A")
            self.lbl_status.setText("Ready")
            self.btn_import.setEnabled(False)
            QMessageBox.critical(self, "Load Error", f"Failed to load image:\n{msg}")
            return

        sz_str = format_size(self.editor.info.size_bytes)
        self.lbl_image_path.setText(f"Image: {os.path.basename(file_path)} ({sz_str})")
        self.lbl_fw_name.setText(f"Firmware: {self.editor.info.firmware_name}")
        self._refresh_asset_list()
        self._update_status()
        self.btn_import.setEnabled(True)

    def _refresh_asset_list(self):
        curr_row = self.asset_list.currentRow()
        
        self.asset_list.blockSignals(True)
        self.asset_list.clear()
        self.asset_list.blockSignals(False)
        
        def sort_key(k):
            if k == "bootlogo.bmp":
                return (0, "")
            if k == "splash/splash.png":
                return (1, "")
            if k == "splash/splash.mp4":
                return (2, "")
            if k == "rootfs/low_pwr.bmp":
                return (3, "")
            if k.startswith("launchimages/"):
                sub = 0
                if k.endswith(".gif"): sub = 0
                elif k.endswith(".jpg"): sub = 1
                elif k.endswith(".mp4"): sub = 2
                elif k.endswith(".ascii"): sub = 3
                return (4, f"{sub}_{k}")
            if k.startswith("themes/"):
                sub = 0
                if "/_art/posters" in k: sub = 0
                else: sub = 1
                return (5, f"{sub}_{k}")
            return (6, k)
            
        sorted_keys = sorted(self.editor.assets.keys(), key=sort_key)
        for key in sorted_keys:
            asset = self.editor.assets[key]
            status = " (Modified)" if asset.is_modified else ""
            if not asset.original_data and not asset.is_modified and not key.startswith("themes/"):
                status = " (Not Set)"
            item = QListWidgetItem(f"{asset.display_name} ({os.path.basename(key)}){status}")
            item.setData(Qt.UserRole, key)
            self.asset_list.addItem(item)
            

        if curr_row >= 0 and curr_row < self.asset_list.count():
            self.asset_list.setCurrentRow(curr_row)
        elif self.asset_list.count() > 0:
            self.asset_list.setCurrentRow(0)


    def _on_asset_changed(self, current, previous):
        if not current:
            self.current_asset_name = None
            self.lbl_orig_preview.clear()
            self.lbl_orig_preview.setText("No Image")
            self.lbl_mod_preview.clear()
            self.lbl_mod_preview.setText("No Image")
            self.lbl_specs.setText("Specs: None")
            return
        
        name = current.data(Qt.UserRole)
        self.current_asset_name = name
        asset = self.editor.assets[name]

        # Update specs label
        size_bytes = len(asset.modified_data) if asset.is_modified else asset.size_bytes
        size_str = format_size(size_bytes)
        if name.endswith(".mp4"):
            self.lbl_specs.setText(f"Specs: Video MP4 | {size_str}")
        elif name.endswith(".ascii"):
            self.lbl_specs.setText(f"Specs: ASCII Plain Text | {size_str}")
        elif name.endswith(".gif"):
            self.lbl_specs.setText(f"Specs: GIF Animation | {size_str}")
        elif name.endswith(".jpg") or name.endswith(".jpeg"):
            self.lbl_specs.setText(f"Specs: JPG Image | {size_str}")
        elif name.endswith(".mp3") or name.endswith(".ogg") or name.endswith(".wav"):
            self.lbl_specs.setText(f"Specs: Audio Sound Effect | {size_str}")
        elif name.startswith("splash/"):
            self.lbl_specs.setText(f"Specs: PNG Image | {size_str}")
        else:
            self.lbl_specs.setText(f"Specs: {asset.width}x{asset.height} | {asset.bpp}-bit BMP | {size_str}")

        # Update previews
        self._update_previews()

    def _update_previews(self):
        if not self.current_asset_name:
            return
            
        name = self.current_asset_name

        asset = self.editor.assets[name]
        
        if name.endswith(".ascii"):

            self.right_stack.setCurrentIndex(1)
            self.btn_import.setEnabled(False)
            self.btn_import.setText("Edit Text Directly")
            
            raw = asset.current_data
            text = raw.decode("utf-8", errors="replace") if raw else ""
            
            self.txt_editor.blockSignals(True)
            self.txt_editor.setPlainText(text)
            self.txt_editor.blockSignals(False)
            return

        self.right_stack.setCurrentIndex(0)
        self.btn_import.setEnabled(True)
        self.btn_import.setText("Import Custom Image...")
        
        if name.endswith(".mp4") or name.endswith(".mp3") or name.endswith(".ogg") or name.endswith(".wav"):

            has_orig = bool(asset.original_data) or (name.startswith("themes/") and asset.size_bytes > 0)
            has_mod = bool(asset.modified_data)
            
            is_audio = name.endswith(".mp3") or name.endswith(".ogg") or name.endswith(".wav")
            label_type = "Audio File" if is_audio else "Video File"
            
            self.lbl_orig_preview.clear()
            self.lbl_orig_preview.setText(f"Original {label_type}\n({'Set' if has_orig else 'Empty / Not Set'})")
            
            self.lbl_mod_preview.clear()
            self.lbl_mod_preview.setText(f"Current {label_type}\n({'Modified' if has_mod else 'Original' if has_orig else 'Empty'})")
            return
        
        orig_pm = self.editor.get_pixmap(name, modified=False)
        mod_pm = self.editor.get_pixmap(name, modified=True)

        # Scale pixmaps to fit preview label size
        def scale_pm(pm):
            if pm.isNull():
                return pm
            return pm.scaled(320, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        orig_pm_scaled = scale_pm(orig_pm)
        mod_pm_scaled = scale_pm(mod_pm)

        if not orig_pm_scaled.isNull():
            self.lbl_orig_preview.setPixmap(orig_pm_scaled)
        elif name.startswith("splash/"):
            self.lbl_orig_preview.setText("No Custom Splash (Empty)")
        else:
            self.lbl_orig_preview.setText("No Image")

        if not mod_pm_scaled.isNull():
            self.lbl_mod_preview.setPixmap(mod_pm_scaled)
        elif name.startswith("splash/"):
            self.lbl_mod_preview.setText("No Custom Splash (Empty)")
        else:
            self.lbl_mod_preview.setText("No Image")

    def _on_text_edited(self):
        if not self.current_asset_name:
            return
        name = self.current_asset_name
        if not name.endswith(".ascii"):
            return
        
        text = self.txt_editor.toPlainText()
        asset = self.editor.assets[name]
        
        new_data = text.encode("utf-8")
        if asset.current_data != new_data:
            asset.modified_data = new_data
            self._update_status()
            
            # Update list item text to say "(Modified)"
            item = self.asset_list.currentItem()
            if item:
                status = " (Modified)" if asset.is_modified else ""
                item.setText(f"{asset.display_name} ({os.path.basename(name)}){status}")

    def _on_import_clicked(self):
        if not self.current_asset_name:
            return
        
        name = self.current_asset_name
        asset = self.editor.assets[name]
        
        if name.endswith(".mp4"):
            file_filter = "Videos (*.mp4)"
            title = "Select Custom Video Splash"
        elif name.endswith(".mp3") or name.endswith(".ogg") or name.endswith(".wav"):
            file_filter = "Audio Files (*.mp3 *.ogg *.wav)"
            title = "Select Custom Audio Sound"
        elif name.startswith("splash/"):
            file_filter = "Images (*.png)"
            title = "Select Custom Image Splash"
        else:
            file_filter = "Images (*.png *.jpg *.jpeg *.bmp)"
            title = "Select Custom Image"

        file_path, _ = QFileDialog.getOpenFileName(self, title, "", file_filter)
        if not file_path:
            return

        print(f"[DEBUG] _on_import_clicked: selected file={file_path} for asset={name}")
        
        # If it's a standard image asset with target dimensions, open crop dialog
        if asset.width > 0 and asset.height > 0 and not name.endswith(".mp4"):
            print(f"[DEBUG] _on_import_clicked: opening crop dialog for {asset.width}x{asset.height}")
            crop_dialog = ImageCropDialog(file_path, asset.width, asset.height, self)
            result = crop_dialog.exec()
            if result == QDialog.Accepted:
                print(f"[DEBUG] _on_import_clicked: crop accepted")
                cropped_img = crop_dialog.get_cropped_image()
                # Save to temp PNG file
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                    temp_path = tf.name
                try:
                    cropped_img.save(temp_path, format="PNG")
                    print(f"[DEBUG] _on_import_clicked: calling replace_asset")
                    ok, msg = self.editor.replace_asset(name, temp_path)
                finally:
                    try:
                        os.remove(temp_path)
                    except:
                        pass
                if not ok:
                    QMessageBox.critical(self, "Import Error", f"Failed to import/convert asset:\n{msg}")
                    return
                # Defer UI updates to let Qt finish cleaning up the modal dialog
                print(f"[DEBUG] _on_import_clicked: deferring UI update via QTimer")
                QTimer.singleShot(50, self._deferred_post_import_update)
            else:
                print(f"[DEBUG] _on_import_clicked: crop cancelled")
        else:
            # Splash video or others: direct import
            print(f"[DEBUG] _on_import_clicked: direct replace_asset")
            ok, msg = self.editor.replace_asset(name, file_path)
            if not ok:
                QMessageBox.critical(self, "Import Error", f"Failed to import/convert asset:\n{msg}")
                return
            # No modal dialog was opened, so we can update immediately
            self._deferred_post_import_update()

    def _deferred_post_import_update(self):
        """Called after import completes, possibly deferred via QTimer to avoid modal dialog deadlock."""
        print(f"[DEBUG] _deferred_post_import_update: starting")
        self._update_previews()
        print(f"[DEBUG] _deferred_post_import_update: previews done")
        self._update_status()
        print(f"[DEBUG] _deferred_post_import_update: status done")
        self._refresh_asset_list()
        print(f"[DEBUG] _deferred_post_import_update: fully finished!")

    def _on_reset_asset_clicked(self):
        if not self.current_asset_name:
            return
        self.editor.reset_asset(self.current_asset_name)
        self._update_previews()
        self._update_status()
        self._refresh_asset_list()

    def _on_reset_all_clicked(self):
        self.editor.reset_all()
        self._update_previews()
        self._update_status()
        self._refresh_asset_list()

    def _on_save_clicked(self):
        if not self.editor.has_changes():
            QMessageBox.information(self, "No Changes", "There are no changes to save.")
            return
        
        reply = QMessageBox.question(
            self, "Confirm Save",
            "Are you sure you want to write these changes back to the backup image file?\n"
            "This will modify the file directly.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        self.lbl_status.setText("Saving changes to image...")
        self.btn_save.setEnabled(False)
        self.btn_reset_all.setEnabled(False)
        self.btn_import.setEnabled(False)
        QApplication.processEvents()
        
        # Run save synchronously (subprocess.run deadlocks when called from QThread on macOS)
        print(f"[DEBUG] _on_save_clicked: starting save_to_image synchronously")
        ok, msg = self.editor.save_to_image()
        print(f"[DEBUG] _on_save_clicked: save_to_image returned ok={ok}, msg={msg}")
        
        self.btn_import.setEnabled(True)
        if ok:
            QMessageBox.information(self, "Success", msg)
        else:
            QMessageBox.critical(self, "Save Error", f"Failed to save changes:\n{msg}")
        # Defer UI updates to let Qt process the modal dialog close
        QTimer.singleShot(50, self._deferred_post_save_update)
    
    def _deferred_post_save_update(self):
        print(f"[DEBUG] _deferred_post_save_update: starting")
        self._update_previews()
        self._update_status()
        self._refresh_asset_list()
        print(f"[DEBUG] _deferred_post_save_update: finished")

    def _on_resize_clicked(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QComboBox
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Resize EASYROMS Partition")
        dialog.setMinimumWidth(380)
        
        layout = QVBoxLayout(dialog)
        
        info = QLabel(
            "<b>Cảnh báo:</b> Việc thay đổi kích thước phân vùng EASYROMS sẽ định dạng lại phân vùng này.<br>"
            "Toàn bộ game hiện có trên phân vùng này sẽ bị xóa.<br>"
            "App sẽ tự động sao lưu cấu trúc thư mục game trống mặc định và phục hồi lại sau khi định dạng.<br><br>"
            "Vui lòng chọn dung lượng thẻ nhớ mục tiêu:"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        
        combo = QComboBox()
        combo.addItem("Dành cho thẻ 16 GB (~15 GB)", 15_000_000_000)
        combo.addItem("Dành cho thẻ 32 GB (~29.5 GB)", 29_500_000_000)
        combo.addItem("Dành cho thẻ 64 GB (~59 GB)", 59_000_000_000)
        combo.addItem("Dành cho thẻ 128 GB (~118 GB)", 118_000_000_000)
        
        # Select closest index
        curr_sz = self.editor.info.size_bytes
        if curr_sz > 100_000_000_000:
            combo.setCurrentIndex(3)
        elif curr_sz > 50_000_000_000:
            combo.setCurrentIndex(2)
        elif curr_sz > 25_000_000_000:
            combo.setCurrentIndex(1)
        else:
            combo.setCurrentIndex(0)
            
        layout.addWidget(combo)
        
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dialog.accept)
        btn_box.rejected.connect(dialog.reject)
        layout.addWidget(btn_box)
        
        if dialog.exec() == QDialog.Accepted:
            target_bytes = combo.currentData()
            
            # Double check confirmation
            reply = QMessageBox.warning(
                self, "Xác nhận Resize",
                "Bạn có thực sự muốn thay đổi kích thước không?\n"
                "Thao tác này sẽ kéo dài/thu ngắn file .img và format lại phân vùng game.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
                
            self.lbl_status.setText("Đang thay đổi kích thước và format lại phân vùng EASYROMS...")
            QApplication.setOverrideCursor(Qt.WaitCursor)
            
            self.editor.reset_all()
            ok, msg = self.editor.resize_easyroms(target_bytes)
            
            # Reload image in editor
            self.editor.load_image(self.editor.info.path)
            QApplication.restoreOverrideCursor()
            
            if ok:
                QMessageBox.information(self, "Thành công", msg)
                self.lbl_image_path.setText(f"Image: {os.path.basename(self.editor.info.path)} ({format_size(self.editor.info.size_bytes)})")
                self._refresh_asset_list()
            else:
                QMessageBox.critical(self, "Lỗi Resize", f"Không thể resize phân vùng:\n{msg}")
            self._update_status()

    def _update_status(self):
        cnt = self.editor.changes_count()
        if cnt > 0:
            self.lbl_status.setText(f"{cnt} pending change(s). Click 'Save Changes' to apply.")
            self.btn_save.setEnabled(True)
            self.btn_reset_all.setEnabled(True)
        else:
            self.lbl_status.setText("No pending changes.")
            self.btn_save.setEnabled(False)
            self.btn_reset_all.setEnabled(False)


# ======================================================================
# Entry point
# ======================================================================

def main():
    app = QApplication(sys.argv)

    # Use native macOS style
    app.setStyle("macOS")

    # Modern, flat stylesheet — dark sidebar, clean table
    app.setStyleSheet("""
        QMainWindow {
            background-color: #ffffff;
        }

        /* ---- Dark sidebar ---- */
        QTreeWidget#sidebar {
            background-color: #1e1e2e;
            color: #cdd6f4;
            border: none;
            font-size: 13px;
            padding: 6px 0;
        }
        QTreeWidget#sidebar::item {
            padding: 5px 10px;
            border-radius: 4px;
            margin: 1px 6px;
        }
        QTreeWidget#sidebar::item:hover {
            background-color: #313244;
        }
        QTreeWidget#sidebar::item:selected {
            background-color: #45475a;
            color: #cdd6f4;
        }
        QTreeWidget#sidebar::branch {
            background-color: #1e1e2e;
        }
        QTreeWidget#sidebar::branch:hover {
            background-color: #313244;
        }
        QLineEdit#sidebarSearch {
            background-color: #11111b;
            color: #cdd6f4;
            border: none;
            padding: 8px 12px;
            font-size: 13px;
            border-bottom: 1px solid #313244;
        }

        /* ---- File table & Navigation ---- */
        QWidget#breadcrumbBar {
            background-color: #f6f8fa;
            border-bottom: 1px solid #d1d9e0;
        }
        QLabel#breadcrumbText {
            font-size: 13px;
            color: #24292f;
            font-weight: 600;
            padding-left: 6px;
        }
        QTableWidget {
            background-color: #ffffff;
            alternate-background-color: #f8f9fa;
            border: none;
            font-size: 13px;
            gridline-color: transparent;
        }
        QTableWidget::item {
            padding: 4px 8px;
        }
        QTableWidget::item:selected {
            background-color: #0969da;
            color: #ffffff;
        }

        /* ---- Column headers ---- */
        QHeaderView::section {
            background-color: #f6f8fa;
            color: #656d76;
            border: none;
            border-bottom: 1px solid #d1d9e0;
            padding: 6px 8px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }

        /* ---- Toolbar ---- */
        QToolBar {
            background-color: #f6f8fa;
            border-bottom: 1px solid #d1d9e0;
            spacing: 4px;
            padding: 2px 6px;
        }
        QToolButton {
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 12px;
        }
        QToolButton:hover {
            background-color: #e1e4e8;
        }
        QToolButton:pressed {
            background-color: #d1d5da;
        }

        /* ---- Status bar ---- */
        QStatusBar {
            background-color: #f6f8fa;
            border-top: 1px solid #d1d9e0;
            font-size: 12px;
            color: #656d76;
        }

        /* ---- Splitter handle ---- */
        QSplitter::handle:horizontal {
            background-color: #d1d9e0;
            width: 1px;
        }

        /* ---- Image Editor Styling ---- */
        QWidget#editorInfoBar {
            background-color: #f6f8fa;
            border: 1px solid #d1d9e0;
            border-radius: 6px;
        }
        QWidget#editorBottomBar {
            background-color: #f6f8fa;
            border-top: 1px solid #d1d9e0;
        }
        QListWidget#assetList {
            background-color: #ffffff;
            border: 1px solid #d1d9e0;
            border-radius: 6px;
            font-size: 13px;
            padding: 4px;
        }
        QListWidget#assetList::item {
            padding: 6px 8px;
            border-radius: 4px;
        }
        QListWidget#assetList::item:hover {
            background-color: #f3f4f6;
        }
        QListWidget#assetList::item:selected {
            background-color: #0969da;
            color: #ffffff;
        }
        QPushButton#btnSaveImage {
            background-color: #1a7f37;
            color: #ffffff;
            font-weight: 600;
            border: 1px solid rgba(27, 31, 36, 0.15);
            border-radius: 6px;
            padding: 5px 12px;
        }
        QPushButton#btnSaveImage:hover {
            background-color: #1b7c35;
        }
        QPushButton#btnSaveImage:disabled {
            background-color: #f6f8fa;
            color: #8c959f;
            border-color: #d1d9e0;
        }
    """)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


class BoxArtLoader(QThread):
    loaded = Signal(str, QPixmap)  # Emits (found_path, pixmap)

    def __init__(self, device, current_path, game_name, lbl_width, lbl_height):
        super().__init__()
        self.device = device
        self.current_path = current_path
        self.game_name = game_name
        self.lbl_width = lbl_width
        self.lbl_height = lbl_height
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        base, _ = os.path.splitext(self.game_name)
        
        path_options = [
            f"/{self.current_path.strip('/')}/downloaded_images/{base}.png",
            f"/{self.current_path.strip('/')}/downloaded_images/{base}.jpg",
            f"/{self.current_path.strip('/')}/images/{base}.png",
            f"/{self.current_path.strip('/')}/images/{base}.jpg",
            f"/{self.current_path.strip('/')}/media/images/{base}.png",
            f"/{self.current_path.strip('/')}/media/images/{base}.jpg",
            f"/{self.current_path.strip('/')}/images/{base}-image.png",
        ]
        
        found_path = None
        for path in path_options:
            if self._is_cancelled:
                return
            if self.device.file_exists(path):
                found_path = path
                break
                
        if found_path and not self._is_cancelled:
            td = tempfile.gettempdir()
            local_tmp = os.path.join(td, f"r36s_temp_{uuid.uuid4().hex}.png")
            if self.device.download_file(found_path, local_tmp):
                if self._is_cancelled:
                    try:
                        os.remove(local_tmp)
                    except:
                        pass
                    return
                try:
                    pil_img = PILImage.open(local_tmp).convert("RGB")
                    import io
                    buf = io.BytesIO()
                    pil_img.save(buf, format="PNG")
                    pm = QPixmap()
                    pm.loadFromData(buf.getvalue())
                except Exception:
                    pm = QPixmap(local_tmp)
                
                try:
                    os.remove(local_tmp)
                except:
                    pass
                
                if not pm.isNull() and not self._is_cancelled:
                    pm_scaled = pm.scaled(
                        self.lbl_width - 4,
                        self.lbl_height - 4,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation
                    )
                    self.loaded.emit(found_path, pm_scaled)
                    return

        if not self._is_cancelled:
            self.loaded.emit("", QPixmap())


class SaveWorker(QThread):
    finished_signal = Signal(bool, str)

    def __init__(self, editor):
        super().__init__()
        self.editor = editor

    def run(self):
        try:
            print(f"[DEBUG] SaveWorker: starting save_to_image")
            ok, msg = self.editor.save_to_image()
            print(f"[DEBUG] SaveWorker: save_to_image returned ok={ok}, msg={msg}")
            self.finished_signal.emit(ok, msg)
        except Exception as exc:
            print(f"[DEBUG] SaveWorker: exception during save: {exc}")
            self.finished_signal.emit(False, f"Save exception: {exc}")


class ImageLoadWorker(QThread):
    finished_signal = Signal(bool, str, str)  # ok, msg, file_path

    def __init__(self, editor, file_path):
        super().__init__()
        self.editor = editor
        self.file_path = file_path

    def run(self):
        try:
            ok, msg = self.editor.load_image(self.file_path)
            self.finished_signal.emit(ok, msg, self.file_path)
        except Exception as exc:
            self.finished_signal.emit(False, f"Load exception: {exc}", self.file_path)


if __name__ == "__main__":
    main()
