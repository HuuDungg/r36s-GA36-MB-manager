"""
R36S SD Card Manager — Desktop application.

A native macOS file manager for R36S handheld game console SD cards.
Reads/writes to hidden FAT32 partitions via mtools, bypassing macOS
mount restrictions.
"""

import sys
import os
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QToolBar, QStatusBar, QFileDialog,
    QMessageBox, QHeaderView, QAbstractItemView, QLabel, QWidget,
    QVBoxLayout, QHBoxLayout, QSizePolicy, QLineEdit, QToolButton,
)
from PySide6.QtCore import Qt, QTimer, QMimeData, Signal
from PySide6.QtGui import (
    QAction, QIcon, QFont, QDragEnterEvent, QDropEvent,
    QKeySequence, QPalette, QColor,
)

from r36s_device import R36SDevice, DeviceInfo, FileEntry, ROM_FILTERS, format_size


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

        right_layout.addWidget(self.table)

        splitter.addWidget(sidebar)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 780])

        self.setCentralWidget(splitter)

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
        self.act_add.setEnabled(connected)
        self.act_delete.setEnabled(connected)
        self.act_eject.setEnabled(connected)
        
        self.btn_back.setEnabled(self.history_index > 0)
        self.btn_forward.setEnabled(self.history_index < len(self.history) - 1)
        self.btn_up.setEnabled(self.current_path != "/" and connected)

        if not connected:
            self.device_label.setText("  No device  ")
            self.device_label.setStyleSheet("color: #999; padding: 0 12px; font-size: 12px;")
            self.free_label.setText("")
            self.lbl_breadcrumb.setText(" Not Connected ")
        else:
            dev = self.device.device
            lbl = dev.device_path.split("/")[-1]
            self.device_label.setText(
                f"  ● {lbl}  ({format_size(dev.size_bytes)})  "
            )
            self.device_label.setStyleSheet(
                "color: #1a7f37; font-weight: 600; padding: 0 12px; font-size: 12px;"
            )
            if self._free_bytes:
                self.free_label.setText(f"  {format_size(self._free_bytes)} free  ")

            # Update breadcrumb label
            parts = self.current_path.strip("/").split("/")
            if not parts or parts == [""]:
                self.lbl_breadcrumb.setText(" R36S SD Card ")
            else:
                self.lbl_breadcrumb.setText(" › ".join(["R36S"] + parts))

    def _set_status(self, msg: str):
        self.status_label.setText(msg)
        QApplication.processEvents()

    # ------------------------------------------------------------------
    # Device scan / connect
    # ------------------------------------------------------------------

    def _initial_scan(self):
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
    """)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
