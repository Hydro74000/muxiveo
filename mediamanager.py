#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Chemin complet optionnel vers l'executable MediaInfo, nom du binaire inclus.
# Exemple : "/usr/bin/mediainfo" ou r"C:\Program Files\MediaInfo\MediaInfo.exe"
# Laisser vide pour conserver l'appel actuel via le PATH : "mediainfo".
MEDIAINFO_EXECUTABLE_PATH = ""

MEDIA_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.webm', '.ts', '.m2ts')

# --- LOGIQUE DE NETTOYAGE ET PARSING ---
TRASH_WORDS = [r'\bmulti\b', r'\bfrench\b', r'\bvff\b', r'\bvfq\b', r'\bvfi\b', r'\bhdr\b', r'\btruefrench\b', r'\bfastsub\b', r'\bvostfr\b']

def clean_title(raw_title):
    if not raw_title:
        return ""
    t = raw_title
    
    # Suppression des tags entre crochets/accolades
    t = re.sub(r'\{.*?\}', '', t)
    t = re.sub(r'\[.*?\]', '', t)
    
    # Remplacement des séparateurs pour isoler les mots et années
    t_clean = t.replace('_', ' ').replace('-', ' ').replace('.', ' ')

    # Séparation si une année est présente (recherche gloutonne pour préserver les années dans le titre)
    m_year = re.search(r'(.*)(\b(19|20)\d{2}\b)', t_clean, re.IGNORECASE)
    if m_year:
        potential = m_year.group(1).strip()
        if re.sub(r'[\s\.\-_]+', '', potential) != "":
            t = potential
    else:
        t = t_clean

    # Nettoyage des résolutions, codecs et mots poubelles
    t = re.split(r'\b(1080p|2160p|720p|bluray|web|vff|multi|vfi|french|dts|x264|x265|hdr|dv|hevc|uhd)\b', t, flags=re.IGNORECASE)[0]
    t = t.replace('.', ' ').replace('-', ' ').replace('_', ' ')
    t = re.sub(r'[\(\)\[\]\{\}]', ' ', t)
    
    for word in TRASH_WORDS:
        t = re.sub(word, '', t, flags=re.IGNORECASE)
        
    return re.sub(r'\s+', ' ', t).strip().title()

def parse_media_file(filepath):
    filename = os.path.basename(filepath)
    name = os.path.splitext(filename)[0]

    if "sample" in name.lower() or (len(name) > 30 and " " not in name and "." not in name and "_" not in name and "-" not in name):
        return None

    # Motifs de détection de séries (Ordre de priorité important)
    series_patterns = [
        r'(?P<show>.*?)[ \.\-_]*[Ss](?P<season>\d+)[ \.\-_]*[Ee](?P<ep>\d+)',              # S01E01, S01.E01
        r'(?P<show>.*?)[ \.\-_]*(?<!\d)(?P<season>\d+)[xX](?P<ep>\d+)(?!\d)',               # 1x01
        r'(?P<show>.*?)[ \.\-_]*[Ss]aison\s*(?P<season>\d+).*?[Ee]pisode\s*(?P<ep>\d+)',    # Saison 1 Episode 2
        r'(?P<show>.*?)[ \.\-_]*[Ss](?P<season>\d+)-(?P<ep>\d+)',                           # S06-01
    ]

    is_series = False
    season_num = 0
    ep_num = 0
    show_title = ""

    # 1. Analyse par nom de fichier
    for pattern in series_patterns:
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            is_series = True
            show_title = match.group('show')
            season_num = int(match.group('season'))
            ep_num = int(match.group('ep'))
            break

    # 2. Analyse par structure de dossier (Fallback)
    if not is_series:
        parent_dir = os.path.basename(os.path.dirname(filepath))
        match_season_dir = re.search(r'^(?:[Ss]eason|[Ss]aison|[Ss])\s*(\d+)\b', parent_dir, re.IGNORECASE)
        if match_season_dir:
            is_series = True
            season_num = int(match_season_dir.group(1))
            # On tente de trouver le numéro d'épisode dans le fichier
            match_ep = re.search(r'(?:[Ee]pisode|[Ee]|^\s*|-|_)\s*(\d{1,3})\b', name, re.IGNORECASE)
            ep_num = int(match_ep.group(1)) if match_ep else 0
            show_title = os.path.basename(os.path.dirname(os.path.dirname(filepath)))

    if is_series:
        clean_show = clean_title(show_title)
        # Si le nom de la série est introuvable via la regex, on prend le dossier parent
        if not clean_show or len(clean_show) < 2:
            clean_show = clean_title(os.path.basename(os.path.dirname(filepath)))
            if re.search(r'(?:[Ss]eason|[Ss]aison|[Ss])\s*\d+', clean_show, re.IGNORECASE):
                clean_show = clean_title(os.path.basename(os.path.dirname(os.path.dirname(filepath))))

        if not clean_show:
            clean_show = "Série Inconnue"
        return {"type": "S", "show": clean_show, "season": season_num, "episode": ep_num, "path": filepath}
    else:
        clean_movie = clean_title(name)
        if clean_movie:
            return {"type": "F", "title": clean_movie, "path": filepath}
        return None

def get_file_size(path) -> str:
    try:
        size_bytes = float(os.path.getsize(path))
        for unit in ['B', 'Ko', 'Mo', 'Go', 'To']:
            if size_bytes < 1024:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.2f} To"
    except Exception:
        return "N/A"

# --- LISTE DES CLÉS MEDIAINFO À CONSERVER ---
TARGET_KEYS = {
    "general": {"Complete name", "File size", "Duration", "Overall bit rate", "Title", "Format"},
    "video": {"Format", "Format profile", "HDR format", "Width", "Height", "Frame rate", "Maximum Content Light Level", "Maximum Frame-Average Light Level", "Bit rate"},
    "audio": {"Format", "Commercial name", "Title", "Language", "Bit rate", "Channel(s)", "Sampling rate"},
    "text": {"Format", "Title", "Language", "Forced"}
}

def _resolve_mediainfo_command():
    if MEDIAINFO_EXECUTABLE_PATH and os.path.isfile(MEDIAINFO_EXECUTABLE_PATH):
        return [MEDIAINFO_EXECUTABLE_PATH]

    which_bin = shutil.which("mediainfo")
    if which_bin:
        return [which_bin]

    try:
        from core.config import AppConfig
        cfg_tool = AppConfig().tool_mediainfo
        if cfg_tool and os.path.isfile(cfg_tool) and os.access(cfg_tool, os.X_OK):
            return [cfg_tool]
    except Exception:
        pass

    candidates = [
        "/var/home/hydromel/dev/MediaInfo/MediaInfo_CLI_CPP/MediaInfo/Project/GNU/CLI/mediainfo",
        "/usr/local/bin/mediainfo",
        "/usr/bin/mediainfo",
        "/home/linuxbrew/.linuxbrew/bin/mediainfo",
    ]
    if sys.platform == "win32":
        candidates.extend([
            r"C:\Program Files\MediaInfo\MediaInfo.exe",
            r"C:\Program Files (x86)\MediaInfo\MediaInfo.exe",
        ])
    for p in candidates:
        if os.path.isfile(p) and (sys.platform == "win32" or os.access(p, os.X_OK)):
            return [p]

    flatpak = shutil.which("flatpak")
    if flatpak:
        return [flatpak, "run", "--command=mediainfo", "net.mediaarea.MediaInfo"]

    return [MEDIAINFO_EXECUTABLE_PATH or "mediainfo"]

# --- THREADS ASYNCHRONES ---
class ScannerThread(QThread):
    file_found = Signal(dict) # Émet un dictionnaire structuré F ou S
    finished = Signal()
    
    def __init__(self, directory):
        super().__init__()
        self.directory = directory

    def run(self):
        for root, _, files in os.walk(self.directory):
            if self.isInterruptionRequested():
                return
            for f in files:
                if self.isInterruptionRequested():
                    return
                if f.lower().endswith(MEDIA_EXTENSIONS):
                    filepath = os.path.join(root, f)
                    meta = parse_media_file(filepath)
                    if meta:
                        self.file_found.emit(meta)
        self.finished.emit()

class MediaInfoThread(QThread):
    info_ready = Signal(list)
    
    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath

    def parse_content(self, text):
        sections_data = []
        lines = text.split('\n')
        current_sec = None
        current_data = {}

        for line in lines:
            line = line.strip()
            if not line:
                continue
            head = line.split(':', 1)[0].strip()
            if ":" not in line or any(head.startswith(s) for s in ["General", "Video", "Audio", "Text", "Menu"]):
                if current_sec and current_data: 
                    sections_data.append({"title": current_sec, "data": current_data})
                current_sec = line
                current_data = {}
                continue
            if ":" in line and current_sec:
                key, val = [x.strip() for x in line.split(':', 1)]
                sec_type = current_sec.lower().split()[0]
                if sec_type in TARGET_KEYS and key in TARGET_KEYS[sec_type]:
                    current_data[key] = val
        if current_sec and current_data: 
            sections_data.append({"title": current_sec, "data": current_data})
        return sections_data

    def run(self):
        base = os.path.splitext(self.filepath)[0]
        nfo_path = base + ".nfo"
        mediainfo_nfo_path = base + "_mediainfo.nfo"

        for candidate, label in [(nfo_path, "📄 Fichier NFO"), (mediainfo_nfo_path, "📄 _mediainfo.nfo")]:
            if os.path.exists(candidate):
                try:
                    with open(candidate, 'r', encoding='utf-8', errors='ignore') as f:
                        data = self.parse_content(f.read())
                        if data:
                            data[0]["data"]["_Source"] = label
                            self.info_ready.emit(data)
                            return
                except Exception:
                    pass

        try:
            cmd = _resolve_mediainfo_command() + [self.filepath]
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', stdin=subprocess.DEVNULL)  # nosec B603
            if res.stdout:
                target = mediainfo_nfo_path if os.path.exists(nfo_path) else nfo_path
                try:
                    with open(target, 'w', encoding='utf-8') as f:
                        f.write(res.stdout)
                except OSError:
                    pass
            data = self.parse_content(res.stdout)
            if data:
                data[0]["data"]["_Source"] = "⚙️ MediaInfo Engine"
            self.info_ready.emit(data)
        except Exception:
            self.info_ready.emit([])

# --- UI COMPONENTS ---
class InfoCard(QFrame):
    def __init__(self, title, data_dict):
        super().__init__()
        color = "#7aa2f7" 
        if "Video" in title:
            color = "#bb9af7" 
        elif "Audio" in title:
            color = "#9ece6a" 
        elif "Text" in title:
            color = "#e0af68" 

        self.setStyleSheet(
            "QFrame { background-color: #24283b; border-radius: 8px; border: 1px solid #414868; margin-bottom: 12px; } QLabel { border: none; }"
        )
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        
        header = QLabel(title.upper())
        header.setStyleSheet(f"color: {color}; font-weight: 900; font-size: 12px; letter-spacing: 1px; margin-bottom: 5px;")
        layout.addWidget(header)
        
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(6)
        
        for k, v in data_dict.items():
            lbl_key = QLabel(k)
            lbl_key.setStyleSheet("color: #565f89; font-weight: bold; font-size: 11px;")
            lbl_val = QLabel(v)
            lbl_val.setStyleSheet("color: #c0caf5; font-size: 11px; font-weight: 600;")
            lbl_val.setWordWrap(True)
            form.addRow(lbl_key, lbl_val)
            
        layout.addLayout(form)

def _startup_directory_from_argv(argv):
    """Retourne le premier dossier positionnel passé au lancement."""
    for raw in argv[1:]:
        if not raw or raw.startswith("-"):
            continue
        path = os.path.abspath(os.path.expanduser(raw))
        if os.path.isdir(path):
            return path
        raise ValueError(f"Le chemin de démarrage n'est pas un dossier valide : {raw}")
    return None


class MediaManager(QMainWindow):
    COL = {"type": 0, "nom": 1, "taille": 2, "action": 3}

    def __init__(self, startup_dir=None):
        super().__init__()
        self.tree_items = {}
        self.full_data = defaultdict(list)
        self.scanned_count = 0
        self._current_path = None
        self._threads = []
        self.scanner = None
        
        self.setWindowTitle("Media Manager")
        self.resize(1400, 900)
        self.apply_styles()
        self.init_ui()

        self.target_dir = startup_dir or QFileDialog.getExistingDirectory(self, "Choisir le dossier de films/séries")
        if self.target_dir:
            self.start_scan()

    def apply_styles(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #1a1b26; color: #c0caf5; font-family: 'Segoe UI', 'Inter', sans-serif; }
            QTreeWidget { background-color: #1f2335; border: 1px solid #292e42; border-radius: 8px; outline: none; }
            QTreeWidget::item { height: 38px; border-bottom: 1px solid #24283b; }
            QTreeWidget::item:selected { background-color: #3d59a1; color: #ffffff; border-radius: 4px; }
            QHeaderView::section { background-color: #1f2335; color: #7aa2f7; padding: 10px; border: none; font-weight: bold; font-size: 11px; text-transform: uppercase; }
            QHeaderView::section:hover { background-color: #252a3a; color: #c0caf5; }
            QHeaderView::section:pressed { background-color: #2d3250; color: #bb9af7; }
            QLineEdit { background-color: #24283b; border: 1px solid #414868; border-radius: 6px; padding: 10px; color: #c0caf5; font-size: 13px; }
            QLineEdit:focus { border: 1px solid #7aa2f7; }
            QPushButton#btn_delete { background-color: #f7768e; color: #1a1b26; border-radius: 4px; font-size: 10px; font-weight: bold; height: 22px; min-width: 75px; max-width: 80px; }
            QPushButton#btn_delete:hover { background-color: #ff9eaf; }
            QPushButton#btn_refresh { background-color: #24283b; border: 1px solid #414868; color: #7aa2f7; border-radius: 6px; padding: 8px 15px; font-weight: bold;}
            QPushButton#btn_refresh:hover { background-color: #414868; }
            QPushButton#btn_browse { background-color: #24283b; border: 1px solid #bb9af7; color: #bb9af7; border-radius: 6px; padding: 8px 15px; font-weight: bold;}
            QPushButton#btn_browse:hover { background-color: #bb9af7; color: #1a1b26; }
            QMenu { background-color: #1f2335; color: #c0caf5; border: 1px solid #414868; border-radius: 6px; padding: 4px; }
            QMenu::item { padding: 6px 20px; border-radius: 4px; }
            QMenu::item:selected { background-color: #f7768e; color: #1a1b26; font-weight: bold; }
            QToolTip { background-color: #24283b; color: #c0caf5; border: 1px solid #414868; padding: 4px; font-size: 11px; }
            QScrollArea { border: none; background-color: transparent; }
            QScrollBar:vertical { background: #1a1b26; width: 12px; margin: 0px; }
            QScrollBar::handle:vertical { background: #414868; border-radius: 6px; min-height: 20px; }
        """)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        top = QHBoxLayout()
        title = QLabel("MEDIA MANAGER")
        title.setStyleSheet("font-weight: 950; font-size: 24px; color: #bb9af7; letter-spacing: -1px;")
        
        self.search = QLineEdit()
        self.search.setPlaceholderText("Rechercher un film ou une série...")
        self.search.setFixedWidth(400)
        self.search.textChanged.connect(self.filter_tree)

        self.btn_refresh = QPushButton("Actualiser")
        self.btn_refresh.setObjectName("btn_refresh")
        self.btn_refresh.clicked.connect(self.start_scan)

        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.setObjectName("btn_browse")
        self.btn_browse.clicked.connect(self.change_directory)
        
        top.addWidget(title)
        top.addStretch()
        top.addWidget(self.search)
        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_browse)
        layout.addLayout(top)

        self.lbl_status = QLabel("Prêt")
        self.lbl_status.setStyleSheet("color: #9ece6a; font-weight: bold; font-size: 12px;")
        layout.addWidget(self.lbl_status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        self.tree = QTreeWidget()
        self.tree.setSortingEnabled(False)
        self.tree.setHeaderLabels(["Type", "Nom / Fichier", "Taille", "Action"])
        self.tree.setColumnWidth(self.COL["type"],   55)
        self.tree.setColumnWidth(self.COL["nom"],   500)
        self.tree.setColumnWidth(self.COL["taille"], 100)
        self.tree.setColumnWidth(self.COL["action"],  95)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)
        self.tree.itemSelectionChanged.connect(self.load_info)

        header = self.tree.header()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self.handle_header_click)

        self._sort_col = None
        self._sort_asc = False
        splitter.addWidget(self.tree)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.details_container = QWidget()
        self.details_layout = QVBoxLayout(self.details_container)
        self.details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.placeholder = self._make_placeholder()
        self.details_layout.addWidget(self.placeholder)

        self.scroll_area.setWidget(self.details_container)
        splitter.addWidget(self.scroll_area)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    def _make_placeholder(self):
        placeholder = QLabel("Sélectionnez un fichier pour voir les détails")
        placeholder.setStyleSheet("color: #565f89; font-style: italic; font-size: 14px;")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return placeholder

    def _clear_details(self):
        while self.details_layout.count():
            item = self.details_layout.takeAt(0)
            if item:
                w = item.widget()
                if w:
                    w.deleteLater()

    def _show_placeholder(self):
        self._clear_details()
        self.placeholder = self._make_placeholder()
        self.details_layout.addWidget(self.placeholder)

    def handle_header_click(self, index):
        C = self.COL
        sortable = {C["type"], C["nom"], C["taille"]}
        if index not in sortable:
            return
        if self._sort_col == index:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_asc = False
        self._sort_col = index

        order = Qt.SortOrder.AscendingOrder if self._sort_asc else Qt.SortOrder.DescendingOrder
        direction = "▲ Croissant" if self._sort_asc else "▼ Décroissant"
        col_name = {C["type"]: "Type", C["nom"]: "Nom / Fichier", C["taille"]: "Taille"}[index]
        self.lbl_status.setText(f"🔃 Tri par {col_name} {direction}…")

        self.tree.header().setSortIndicator(index, order)
        self._update_header_style(index)

        if index == C["type"]:
            self.sort_by_type()
        elif index == C["nom"]:
            self.sort_tree()
        elif index == C["taille"]:
            self.sort_by_size()

    def _update_header_style(self, active_col):
        C = self.COL
        arrow = " ▲" if self._sort_asc else " ▼"
        labels = {C["type"]: "Type", C["nom"]: "Nom / Fichier", C["taille"]: "Taille", C["action"]: "Action"}
        for col, label in labels.items():
            self.tree.headerItem().setText(col, label + (arrow if col == active_col else ""))

        base = "QHeaderView::section { background-color: #1f2335; color: #7aa2f7; padding: 10px; border: none; font-weight: bold; font-size: 11px; }"
        active = "color: #bb9af7; background-color: #1d1f33; border-bottom: 2px solid #bb9af7;"
        nth = {C["type"]: "first", C["nom"]: "nth-child(2)", C["taille"]: "nth-child(3)"}
        extra = f"QHeaderView::section:{nth[active_col]} {{ {active} }}" if active_col in nth else ""
        self.tree.header().setStyleSheet(base + extra)

    def restore_buttons(self, item):
        self.create_del_btn(item)
        for i in range(item.childCount()):
            self.restore_buttons(item.child(i))

    def _make_file_item(self, parent: QTreeWidgetItem, path: str, total_bytes: int) -> QTreeWidgetItem:
        child = QTreeWidgetItem(parent, ["", os.path.basename(path), get_file_size(path), ""])
        child.setData(self.COL["type"], Qt.ItemDataRole.UserRole, path)
        child.setData(self.COL["taille"], Qt.ItemDataRole.UserRole, total_bytes)
        self.create_del_btn(child)
        return child

    def create_del_btn(self, item, path=None):
        btn = QPushButton("Supprimer")
        btn.setObjectName("btn_delete")

        user_data = item.data(self.COL["type"], Qt.ItemDataRole.UserRole)
        tooltip = "Supprimer cet élément"
        if isinstance(user_data, str):
            if user_data.startswith("F|"):
                tooltip = "Supprimer ce film complet"
            elif user_data.startswith("S|"):
                parts = user_data.split("|")
                if len(parts) == 2:
                    tooltip = "Supprimer cette série complète"
                elif len(parts) == 3:
                    tooltip = f"Supprimer la Saison {int(parts[2]):02d} complète"
                elif len(parts) == 4:
                    tooltip = f"Supprimer l'Épisode {int(parts[3]):02d}"
            elif user_data.lower().endswith(MEDIA_EXTENSIONS):
                tooltip = "Supprimer ce fichier vidéo"
        btn.setToolTip(tooltip)

        btn.clicked.connect(lambda checked=False, i=item: self.confirm_delete_item(i))
        c = QWidget()
        layout = QHBoxLayout(c)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(btn)
        self.tree.setItemWidget(item, self.COL["action"], c)
        
    def sort_by_type(self):
        self.tree.setUpdatesEnabled(False)
        items = []
        while self.tree.topLevelItemCount() > 0:
            items.append(self.tree.takeTopLevelItem(0))

        # F avant S si ascendant, S avant F si descendant
        C = self.COL
        items.sort(key=lambda x: (x.text(C["type"]), x.text(C["nom"]).lower()), reverse=not self._sort_asc)

        for parent in items:
            self.tree.addTopLevelItem(parent)
            self.restore_buttons(parent)

        self.tree.setUpdatesEnabled(True)
        self.lbl_status.setText("🔠 Trié par type (F / S)")

    def sort_by_size(self):
        self.tree.setUpdatesEnabled(False)
        items = []
        while self.tree.topLevelItemCount() > 0:
            items.append(self.tree.takeTopLevelItem(0))

        C = self.COL
        sign = 1 if self._sort_asc else -1
        items.sort(key=lambda item: sign * (item.data(C["taille"], Qt.ItemDataRole.UserRole) or 0))

        for parent in items:
            self.tree.addTopLevelItem(parent)
            self.restore_buttons(parent)

        self.tree.setUpdatesEnabled(True)
        self.lbl_status.setText("⚖️ Trié par taille")

    def sort_tree(self):
        self.tree.setUpdatesEnabled(False)
        items = []
        while self.tree.topLevelItemCount() > 0:
            items.append(self.tree.takeTopLevelItem(0))

        # 1. Sous-tri croissant saisons et épisodes par numéro
        C = self.COL
        def sort_children_by_num(node):
            children = [node.takeChild(0) for _ in range(node.childCount())]
            children.sort(key=lambda x: int(m.group()) if (m := re.search(r'\d+', x.text(C["nom"]))) else 0)
            for child in children:
                node.addChild(child)

        for parent in items:
            sort_children_by_num(parent)          # saisons
            for i in range(parent.childCount()):
                sort_children_by_num(parent.child(i))  # épisodes

        # 2. Tri du niveau parent (Doublons en premier, puis alphabétique)
        items.sort(key=lambda x: x.text(C["nom"]).lower(), reverse=not self._sort_asc)
        items.sort(key=lambda x: (x.data(C["action"], Qt.ItemDataRole.UserRole) or 0), reverse=True)
        
        for parent in items:
            self.tree.addTopLevelItem(parent)
            self.restore_buttons(parent)
        
        self.tree.setUpdatesEnabled(True)
        self.lbl_status.setText(f"✅ Tri terminé : {len(items)} titres/séries organisés.")

    def start_scan(self):
        if self.scanner is not None and self.scanner.isRunning():
            self.scanner.requestInterruption()
            self.scanner.wait(2000)

        self.tree.clear()
        self.tree_items.clear()
        self.full_data.clear()
        self.scanned_count = 0
        
        self.lbl_status.setText(f"⏳ Analyse du dossier en cours... ({self.target_dir})")
        self.scanner = ScannerThread(self.target_dir)
        self.scanner.file_found.connect(self.add_to_ui)
        self.scanner.finished.connect(self.on_scan_finished)
        self.scanner.start()

    def on_scan_finished(self):
        self.lbl_status.setText(f"✅ Analyse terminée : {self.tree.topLevelItemCount()} éléments racines en mémoire.")
        self._sort_col = self.COL["nom"]
        self._sort_asc = False
        self.tree.header().setSortIndicator(self.COL["nom"], Qt.SortOrder.DescendingOrder)
        self._update_header_style(self.COL["nom"])
        self.sort_tree()

    def add_to_ui(self, meta):
        path = meta["path"]
        total_bytes = os.path.getsize(path) if os.path.exists(path) else 0
        self.scanned_count += 1
        
        if self.scanned_count % 10 == 0:
            self.lbl_status.setText(f"⏳ Analyse en cours... ({self.scanned_count} fichiers trouvés)")

        C = self.COL
        if meta["type"] == "F":
            title = meta["title"]
            key_f = f"F|{title}"

            self.full_data[key_f].append(path)
            count = len(self.full_data[key_f])

            if key_f not in self.tree_items:
                parent = QTreeWidgetItem(self.tree, ["F", f"{title} ({count})", self.format_size(total_bytes), ""])
                parent.setForeground(C["type"], QColor("#9ece6a"))
                parent.setTextAlignment(C["type"], Qt.AlignmentFlag.AlignCenter)
                parent.setFont(C["nom"], QFont("Segoe UI", 11, QFont.Weight.Bold))
                parent.setData(C["taille"], Qt.ItemDataRole.UserRole, total_bytes)
                parent.setData(C["action"], Qt.ItemDataRole.UserRole, count - 1)
                parent.setData(C["type"], Qt.ItemDataRole.UserRole, key_f)
                self.tree_items[key_f] = parent
                self.create_del_btn(parent)

                current_search = self.search.text().lower()
                if current_search and current_search not in title.lower():
                    parent.setHidden(True)
            else:
                parent = self.tree_items[key_f]
                parent.setText(C["nom"], f"{title} ({count})")
                new_size = parent.data(C["taille"], Qt.ItemDataRole.UserRole) + total_bytes
                parent.setData(C["taille"], Qt.ItemDataRole.UserRole, new_size)
                parent.setText(C["taille"], self.format_size(new_size))
                parent.setData(C["action"], Qt.ItemDataRole.UserRole, count - 1)
                if count > 1:
                    parent.setForeground(C["nom"], QColor("#e0af68"))

            assert isinstance(parent, QTreeWidgetItem)
            self._make_file_item(parent, path, total_bytes)

        elif meta["type"] == "S":
            show = meta["show"]
            season = meta["season"]
            ep = meta["episode"]

            key_s = f"S|{show}"
            key_season = f"S|{show}|{season}"
            key_ep = f"S|{show}|{season}|{ep}"

            self.full_data[key_ep].append(path)
            count = len(self.full_data[key_ep])

            # 1. SHOW
            if key_s not in self.tree_items:
                n_show = QTreeWidgetItem(self.tree, ["S", show, "0 B", ""])
                n_show.setForeground(C["type"], QColor("#e0af68"))
                n_show.setTextAlignment(C["type"], Qt.AlignmentFlag.AlignCenter)
                n_show.setFont(C["nom"], QFont("Segoe UI", 11, QFont.Weight.Bold))
                n_show.setForeground(C["nom"], QColor("#bb9af7"))
                n_show.setData(C["taille"], Qt.ItemDataRole.UserRole, 0)
                n_show.setData(C["action"], Qt.ItemDataRole.UserRole, 0)
                n_show.setData(C["type"], Qt.ItemDataRole.UserRole, key_s)
                self.tree_items[key_s] = n_show
                self.create_del_btn(n_show)

                current_search = self.search.text().lower()
                if current_search and current_search not in show.lower():
                    n_show.setHidden(True)
            else:
                n_show = self.tree_items[key_s]

            s_size = n_show.data(C["taille"], Qt.ItemDataRole.UserRole) + total_bytes
            n_show.setData(C["taille"], Qt.ItemDataRole.UserRole, s_size)
            n_show.setText(C["taille"], self.format_size(s_size))
            if count > 1:
                n_show.setData(C["action"], Qt.ItemDataRole.UserRole, n_show.data(C["action"], Qt.ItemDataRole.UserRole) + 1)

            # 2. SEASON
            if key_season not in self.tree_items:
                n_season = QTreeWidgetItem(n_show, ["", f"Saison {season:02d} (0 épisode)", "0 B", ""])
                n_season.setFont(C["nom"], QFont("Segoe UI", 10, QFont.Weight.Bold))
                n_season.setForeground(C["nom"], QColor("#7aa2f7"))
                n_season.setData(C["taille"], Qt.ItemDataRole.UserRole, 0)
                n_season.setData(C["type"], Qt.ItemDataRole.UserRole, key_season)
                self.tree_items[key_season] = n_season
                self.create_del_btn(n_season)
            else:
                n_season = self.tree_items[key_season]

            seas_size = n_season.data(C["taille"], Qt.ItemDataRole.UserRole) + total_bytes
            n_season.setData(C["taille"], Qt.ItemDataRole.UserRole, seas_size)
            n_season.setText(C["taille"], self.format_size(seas_size))

            nb_saisons = n_show.childCount()
            n_show.setText(C["nom"], f"{show} ({nb_saisons} {'saison' if nb_saisons <= 1 else 'saisons'})")

            # 3. EPISODE
            if key_ep not in self.tree_items:
                n_ep = QTreeWidgetItem(n_season, ["", f"Épisode {ep:02d} ({count})", "0 B", ""])
                n_ep.setFont(C["nom"], QFont("Segoe UI", 10))
                n_ep.setData(C["taille"], Qt.ItemDataRole.UserRole, 0)
                n_ep.setData(C["type"], Qt.ItemDataRole.UserRole, key_ep)
                self.tree_items[key_ep] = n_ep
                self.create_del_btn(n_ep)
            else:
                n_ep = self.tree_items[key_ep]
                n_ep.setText(C["nom"], f"Épisode {ep:02d} ({count})")
                if count > 1:
                    n_ep.setForeground(C["nom"], QColor("#e0af68"))

            nb_eps = n_season.childCount()
            n_season.setText(C["nom"], f"Saison {season:02d} ({nb_eps} {'épisode' if nb_eps <= 1 else 'épisodes'})")

            ep_size = n_ep.data(C["taille"], Qt.ItemDataRole.UserRole) + total_bytes
            n_ep.setData(C["taille"], Qt.ItemDataRole.UserRole, ep_size)
            n_ep.setText(C["taille"], self.format_size(ep_size))

            # 4. FILE
            assert isinstance(n_ep, QTreeWidgetItem)
            self._make_file_item(n_ep, path, total_bytes)

    def format_size(self, size_bytes):
        if size_bytes == 0:
            return "0 B"
        for unit in ['B', 'Ko', 'Mo', 'Go', 'To']:
            if size_bytes < 1024:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.2f} To"

    def change_directory(self):
        new_path = QFileDialog.getExistingDirectory(self, "Sélectionner un nouveau répertoire")
        if new_path and new_path != self.target_dir:
            self.target_dir = new_path
            self.start_scan()
            
    def filter_tree(self, text):
        query = text.strip().lower()
        if not query:
            for i in range(self.tree.topLevelItemCount()):
                parent = self.tree.topLevelItem(i)
                if parent is not None:
                    parent.setHidden(False)
            return

        def matches(node):
            if query in node.text(self.COL["nom"]).lower():
                return True
            for j in range(node.childCount()):
                child = node.child(j)
                if child and matches(child):
                    return True
            return False

        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            if parent is None:
                continue
            parent.setHidden(not matches(parent))

    def load_info(self):
        sel = self.tree.selectedItems()
        if not sel or not sel[0].data(self.COL["type"], Qt.ItemDataRole.UserRole):
            return

        path = sel[0].data(self.COL["type"], Qt.ItemDataRole.UserRole)
        if not isinstance(path, str) or not path.lower().endswith(MEDIA_EXTENSIONS):
            return

        self._current_path = path

        self._clear_details()

        loader = QLabel("⚡ Extraction des métadonnées MediaInfo...")
        loader.setStyleSheet("color: #7aa2f7; font-weight: bold; font-size: 12px; margin-top: 20px;")
        loader.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details_layout.addWidget(loader)

        t = MediaInfoThread(path)
        t.info_ready.connect(lambda sections, p=path: self._on_info_ready(sections, p))
        t.finished.connect(lambda thread=t: self._cleanup_thread(thread))
        self._threads.append(t)
        t.start()

    def _cleanup_thread(self, thread):
        if thread in self._threads:
            self._threads.remove(thread)

    def _on_info_ready(self, sections, path):
        if path != self._current_path:
            return
        self.draw_info(sections)

    def draw_info(self, sections):
        self._clear_details()
            
        if not sections:
            err = QLabel("Aucune donnée MediaInfo trouvée.")
            err.setStyleSheet("color: #f7768e;")
            self.details_layout.addWidget(err)
            
        for sec in sections:
            if sec["data"]: 
                self.details_layout.addWidget(InfoCard(sec["title"], sec["data"]))
                
        self.details_layout.addStretch()

    def show_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return

        self.tree.setCurrentItem(item)
        user_data = item.data(self.COL["type"], Qt.ItemDataRole.UserRole)
        if not user_data:
            return

        menu = QMenu(self)
        if isinstance(user_data, str) and user_data.startswith("F|"):
            title = user_data.split("|", 1)[1]
            action_text = f"🗑️ Supprimer le film complet « {title} »..."
        elif isinstance(user_data, str) and user_data.startswith("S|"):
            parts = user_data.split("|")
            if len(parts) == 2:
                action_text = f"🗑️ Supprimer la série complète « {parts[1]} »..."
            elif len(parts) == 3:
                action_text = f"🗑️ Supprimer la Saison {int(parts[2]):02d} complète..."
            elif len(parts) == 4:
                action_text = f"🗑️ Supprimer l'Épisode {int(parts[3]):02d}..."
            else:
                action_text = "🗑️ Supprimer l'élément..."
        elif isinstance(user_data, str) and user_data.lower().endswith(MEDIA_EXTENSIONS):
            action_text = f"🗑️ Supprimer le fichier « {os.path.basename(user_data)} »..."
        else:
            action_text = "🗑️ Supprimer l'élément..."

        delete_action = menu.addAction(action_text)
        delete_action.triggered.connect(lambda: self.confirm_delete_item(item))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _get_files_under_item(self, item: QTreeWidgetItem) -> list[tuple[str, QTreeWidgetItem]]:
        """Retourne récursivement tous les tuples (filepath, file_item) sous un nœud."""
        user_data = item.data(self.COL["type"], Qt.ItemDataRole.UserRole)
        if isinstance(user_data, str) and user_data.lower().endswith(MEDIA_EXTENSIONS):
            return [(user_data, item)]

        files = []
        for i in range(item.childCount()):
            child = item.child(i)
            if child:
                files.extend(self._get_files_under_item(child))
        return files

    def _ask_delete_confirmation(self, title: str, text: str) -> bool:
        msg = QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setText(text)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        msg.setStyleSheet("QLabel{color: #c0caf5; min-width: 450px; font-size: 12px;} QPushButton{min-width: 80px;}")
        return msg.exec() == QMessageBox.StandardButton.Yes

    def _reduce_size_upwards(self, node, amount):
        C = self.COL
        while node:
            cur = node.data(C["taille"], Qt.ItemDataRole.UserRole) or 0
            new_val = max(0, cur - amount)
            node.setData(C["taille"], Qt.ItemDataRole.UserRole, new_val)
            node.setText(C["taille"], self.format_size(new_val))
            node = node.parent()

    def _cleanup_empty_show(self, show_node):
        C = self.COL
        if show_node.childCount() == 0:
            top_idx = self.tree.indexOfTopLevelItem(show_node)
            if top_idx >= 0:
                self.tree.takeTopLevelItem(top_idx)
            show_key = show_node.data(C["type"], Qt.ItemDataRole.UserRole)
            self.tree_items.pop(show_key, None)
        else:
            nb_saisons = show_node.childCount()
            show_key = show_node.data(C["type"], Qt.ItemDataRole.UserRole) or ""
            s_name = show_key.split('|', 1)[1] if '|' in show_key else show_key
            show_node.setText(C["nom"], f"{s_name} ({nb_saisons} {'saison' if nb_saisons <= 1 else 'saisons'})")

    def _cleanup_empty_season(self, season_node, show_node):
        C = self.COL
        if season_node.childCount() == 0 and show_node is not None:
            show_node.removeChild(season_node)
            season_key = season_node.data(C["type"], Qt.ItemDataRole.UserRole)
            self.tree_items.pop(season_key, None)
            self._cleanup_empty_show(show_node)
        elif season_node.childCount() > 0:
            nb_eps = season_node.childCount()
            season_key = season_node.data(C["type"], Qt.ItemDataRole.UserRole) or ""
            s_num = int(season_key.split('|')[-1]) if '|' in season_key else 0
            season_node.setText(C["nom"], f"Saison {s_num:02d} ({nb_eps} {'épisode' if nb_eps <= 1 else 'épisodes'})")

    def confirm_delete_item(self, item: QTreeWidgetItem) -> bool:
        """Supprime un fichier, un épisode, une saison, une série ou un film avec confirmation détaillée."""
        files_to_delete = self._get_files_under_item(item)
        if not files_to_delete:
            return False

        C = self.COL
        user_data = item.data(C["type"], Qt.ItemDataRole.UserRole)
        nb_files = len(files_to_delete)

        total_bytes = 0
        existing_files = []
        for path, file_item in files_to_delete:
            size = os.path.getsize(path) if os.path.isfile(path) else 0
            total_bytes += size
            existing_files.append((path, file_item, size))

        if total_bytes == 0:
            total_bytes = item.data(C["taille"], Qt.ItemDataRole.UserRole) or 0
        formatted_size = self.format_size(total_bytes)

        # Construction du message de confirmation selon le niveau hiérarchique
        if isinstance(user_data, str) and user_data.startswith("S|"):
            parts = user_data.split("|")
            if len(parts) == 2:
                # Série complète
                show_name = parts[1]
                nb_seasons = item.childCount()
                dialog_title = "Supprimer la série complète"
                dialog_text = (
                    f"Voulez-vous vraiment supprimer définitivement la série complète :\n"
                    f"« {show_name} » ?\n\n"
                    f"• Nombre de saisons : {nb_seasons}\n"
                    f"• Fichiers vidéo : {nb_files}\n"
                    f"• Espace disque à libérer : {formatted_size}\n\n"
                    f"⚠️ Cette action est irréversible et supprimera tous les fichiers associés sur le disque."
                )
            elif len(parts) == 3:
                # Saison complète
                show_name, s_num = parts[1], int(parts[2])
                nb_eps = item.childCount()
                dialog_title = "Supprimer la saison complète"
                dialog_text = (
                    f"Voulez-vous vraiment supprimer définitivement la saison :\n"
                    f"« {show_name} - Saison {s_num:02d} » ?\n\n"
                    f"• Nombre d'épisodes : {nb_eps}\n"
                    f"• Fichiers vidéo : {nb_files}\n"
                    f"• Espace disque à libérer : {formatted_size}\n\n"
                    f"⚠️ Cette action est irréversible et supprimera tous les fichiers associés sur le disque."
                )
            elif len(parts) == 4:
                # Épisode
                show_name, s_num, ep_num = parts[1], int(parts[2]), int(parts[3])
                dialog_title = "Supprimer l'épisode"
                dialog_text = (
                    f"Voulez-vous vraiment supprimer définitivement l'épisode :\n"
                    f"« {show_name} - S{s_num:02d}E{ep_num:02d} » ?\n\n"
                    f"• Fichiers vidéo ({nb_files}) : {formatted_size}\n\n"
                    f"⚠️ Cette action est irréversible."
                )
            else:
                dialog_title = "Supprimer l'élément"
                dialog_text = f"Voulez-vous supprimer cet élément ({formatted_size}) ?"
        elif isinstance(user_data, str) and user_data.startswith("F|"):
            # Film complet
            movie_title = user_data.split("|", 1)[1]
            dup_info = f"\n• Fichiers / versions : {nb_files}" if nb_files > 1 else ""
            dialog_title = "Supprimer le film complet"
            dialog_text = (
                f"Voulez-vous vraiment supprimer définitivement le film :\n"
                f"« {movie_title} » ?{dup_info}\n"
                f"• Espace disque à libérer : {formatted_size}\n\n"
                f"⚠️ Cette action est irréversible."
            )
        else:
            # Fichier unique
            filename = item.text(C["nom"])
            dialog_title = "Supprimer le fichier"
            dialog_text = (
                f"Voulez-vous vraiment supprimer définitivement ce fichier ?\n\n"
                f"{filename}\n"
                f"• Espace disque à libérer : {formatted_size}\n\n"
                f"⚠️ Cette action est irréversible."
            )

        if not self._ask_delete_confirmation(dialog_title, dialog_text):
            return False

        # 1. Suppression physique des fichiers et métadonnées
        errors = []
        deleted_paths = set()
        affected_directories = set()

        for path, _, _ in existing_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
                deleted_paths.add(path)
                affected_directories.add(os.path.dirname(path))

                # Nettoyage des caches NFO associés
                base = os.path.splitext(path)[0]
                for nfo_candidate in (base + "_mediainfo.nfo", base + ".nfo"):
                    if os.path.isfile(nfo_candidate):
                        try:
                            os.remove(nfo_candidate)
                        except OSError:
                            pass
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")

        if errors:
            QMessageBox.critical(
                self,
                "Erreur lors de la suppression",
                "Des erreurs sont survenues lors de la suppression :\n\n" + "\n".join(errors[:5]),
            )
            if not deleted_paths:
                return False

        # Nettoyage des dossiers devenus vides (sans supprimer target_dir)
        for folder in sorted(affected_directories, key=len, reverse=True):
            cur = os.path.abspath(folder)
            stop = os.path.abspath(self.target_dir) if self.target_dir else None
            while cur and cur != stop and cur != os.path.dirname(cur):
                try:
                    os.rmdir(cur)
                    cur = os.path.dirname(cur)
                except OSError:
                    break

        # 2. Mise à jour de self.full_data
        for p in deleted_paths:
            for k in list(self.full_data.keys()):
                if p in self.full_data[k]:
                    self.full_data[k].remove(p)
                    if not self.full_data[k]:
                        del self.full_data[k]

        # 3. Mise à jour de l'arbre selon le type d'élément supprimé
        if item.parent() is None:
            # Élément racine (Film complet ou Série complète)
            top_idx = self.tree.indexOfTopLevelItem(item)
            if top_idx >= 0:
                self.tree.takeTopLevelItem(top_idx)
            if isinstance(user_data, str):
                for k in [k for k in self.tree_items if k == user_data or k.startswith(f"{user_data}|")]:
                    self.tree_items.pop(k, None)

        elif isinstance(user_data, str) and user_data.startswith("S|") and len(user_data.split("|")) == 3:
            # Saison complète
            show_node = item.parent()
            season_key = user_data
            season_size = item.data(C["taille"], Qt.ItemDataRole.UserRole) or 0
            if show_node:
                show_node.removeChild(item)
                self._reduce_size_upwards(show_node, season_size)
                for k in [k for k in self.tree_items if k == season_key or k.startswith(f"{season_key}|")]:
                    self.tree_items.pop(k, None)
                self._cleanup_empty_show(show_node)

        elif isinstance(user_data, str) and user_data.startswith("S|") and len(user_data.split("|")) == 4:
            # Épisode
            season_node = item.parent()
            show_node = season_node.parent() if season_node else None
            ep_key = user_data
            ep_size = item.data(C["taille"], Qt.ItemDataRole.UserRole) or 0
            if season_node:
                season_node.removeChild(item)
                self._reduce_size_upwards(season_node, ep_size)
                self.tree_items.pop(ep_key, None)
                self._cleanup_empty_season(season_node, show_node)

        else:
            # Fichier unique
            parent_node = item.parent()
            if parent_node is None:
                return True
            file_size = item.data(C["taille"], Qt.ItemDataRole.UserRole) or 0
            data_key = parent_node.data(C["type"], Qt.ItemDataRole.UserRole)

            parent_node.removeChild(item)
            self._reduce_size_upwards(parent_node, file_size)

            season_node = parent_node.parent() if parent_node.parent() else None
            show_node = season_node.parent() if season_node else None

            if parent_node.childCount() == 0:
                if season_node:  # Épisode devenu vide
                    season_node.removeChild(parent_node)
                    self.tree_items.pop(data_key, None)
                    self._cleanup_empty_season(season_node, show_node)
                else:  # Film devenu vide
                    top_idx = self.tree.indexOfTopLevelItem(parent_node)
                    if top_idx >= 0:
                        self.tree.takeTopLevelItem(top_idx)
                    self.tree_items.pop(data_key, None)
            else:
                # Il reste des doublons / versions
                remaining = parent_node.childCount()
                if season_node:
                    ep_num = int(data_key.split('|')[-1]) if '|' in data_key else 0
                    parent_node.setText(C["nom"], f"Épisode {ep_num:02d} ({remaining})")
                    if remaining == 1:
                        parent_node.setForeground(C["nom"], QColor("#c0caf5"))
                    if show_node:
                        show_node.setData(C["action"], Qt.ItemDataRole.UserRole, max(0, (show_node.data(C["action"], Qt.ItemDataRole.UserRole) or 0) - 1))
                else:
                    title = data_key.split('|', 1)[1] if '|' in data_key else data_key
                    parent_node.setText(C["nom"], f"{title} ({remaining})")
                    if remaining == 1:
                        parent_node.setForeground(C["nom"], QColor("#c0caf5"))
                    parent_node.setData(C["action"], Qt.ItemDataRole.UserRole, remaining - 1)

        # 4. Nettoyage de l'interface
        if self._current_path in deleted_paths:
            self._current_path = None
            self._show_placeholder()

        self.lbl_status.setText(
            f"🗑️ Suppression effectuée : {len(deleted_paths)} fichier(s) supprimé(s) ({self.format_size(total_bytes)} libérés)."
        )
        return True

    def confirm_delete(self, path, item):
        """Méthode de compatibilité pour la suppression d'un élément."""
        return self.confirm_delete_item(item)

    def closeEvent(self, event):
        if self.scanner is not None and self.scanner.isRunning():
            self.scanner.requestInterruption()
            self.scanner.wait(1000)
        for t in list(self._threads):
            if t.isRunning():
                t.requestInterruption()
                t.wait(1000)
        super().closeEvent(event)

if __name__ == "__main__":
    if "-h" in sys.argv or "--help" in sys.argv:
        print("Usage: mediamanager.py [OPTIONS] [DIRECTORY]")
        print("\nParcourt et organise une médiathèque de films et séries, affiche les métadonnées MediaInfo et gère les doublons.")
        print("\nArguments:")
        print("  DIRECTORY    Dossier racine contenant les médias (optionnel)")
        print("\nOptions:")
        print("  -h, --help   Affiche ce message d'aide et quitte")
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    try:
        startup_dir = _startup_directory_from_argv(sys.argv)
    except ValueError as exc:
        QMessageBox.critical(None, "Dossier invalide", str(exc))
        sys.exit(2)
    win = MediaManager(startup_dir)
    if not win.target_dir:
        sys.exit(0)
    win.show()
    sys.exit(app.exec())
