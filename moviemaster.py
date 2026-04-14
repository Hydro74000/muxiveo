import os
import re
import subprocess
import sys
from collections import defaultdict

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QTreeWidget, QTreeWidgetItem, QPushButton, QLabel, 
    QMessageBox, QSplitter, QFileDialog, QLineEdit, QScrollArea, QFrame, QFormLayout, QSizePolicy
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QColor

# --- LOGIQUE DE NETTOYAGE ---
TRASH_WORDS = [r'\bmulti\b', r'\bfrench\b', r'\bvff\b', r'\bvfq\b', r'\bvfi\b', r'\bhdr\b', r'\btruefrench\b']

def clean_movie_name(filename):
    name = os.path.splitext(filename)[0]
    if "sample" in name.lower() or (len(name) > 30 and " " not in name and "." not in name):
        return None
    name = re.sub(r'\{.*?\}', '', name)
    match = re.search(r'(.*?)(\b(19|20)\d{2}\b)', name, re.IGNORECASE)
    if match:
        potential_title = match.group(1).strip()
        if not potential_title or potential_title.replace('.', '').replace('-', '') == "":
            after_year = name[match.end():]
            potential_title = re.split(r'\b(1080p|2160p|720p|bluray|web|vff|multi)\b', after_year, flags=re.IGNORECASE)[0]
    else:
        potential_title = re.split(r'\b(1080p|2160p|720p|bluray|multi|vff|vfi|french|dts|x264|x265)\b', name, flags=re.IGNORECASE)[0]
    
    clean_name = potential_title.replace('.', ' ').replace('-', ' ').replace('_', ' ')
    for word in TRASH_WORDS:
        clean_name = re.sub(word, '', clean_name, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', clean_name).strip().title()

def get_file_size(path):
    try:
        size_bytes = os.path.getsize(path)
        for unit in ['B', 'Ko', 'Mo', 'Go', 'To']:
            if size_bytes < 1024: return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024
    except: return "N/A"

# --- LISTE DES CLÉS MEDIAINFO À CONSERVER ---
TARGET_KEYS = {
    "general": {"Complete name", "File size", "Duration", "Overall bit rate", "Title", "Format"},
    "video": {"Format", "Format profile", "HDR format", "Width", "Height", "Frame rate", "Maximum Content Light Level", "Maximum Frame-Average Light Level", "Bit rate"},
    "audio": {"Format", "Commercial name", "Title", "Language", "Bit rate", "Channel(s)", "Sampling rate"},
    "text": {"Format", "Title", "Language", "Forced"}
}

# --- THREADS ASYNCHRONES ---
class ScannerThread(QThread):
    file_found = pyqtSignal(str, str) # Émet le film dès qu'il est trouvé
    finished = pyqtSignal()
    
    def __init__(self, directory):
        super().__init__()
        self.directory = directory

    def run(self):
        for root, _, files in os.walk(self.directory):
            for f in files:
                if f.lower().endswith(('.mkv', '.mp4', '.avi', '.mov')):
                    title = clean_movie_name(f)
                    if title:
                        self.file_found.emit(title, os.path.join(root, f))
        self.finished.emit()

class MediaInfoThread(QThread):
    info_ready = pyqtSignal(list)
    
    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath

    def run(self):
        sections_data = []
        try:
            res = subprocess.run(['mediainfo', self.filepath], capture_output=True, text=True, encoding='utf-8')
            lines = res.stdout.split('\n')
            current_sec = None
            current_data = {}

            for line in lines:
                line = line.strip()
                if not line: continue
                
                # Nouvelle section détectée
                if ":" not in line or (line.split(':', 1)[0].strip() in ["General", "Video", "Audio", "Text"]):
                    if current_sec and current_data: 
                        sections_data.append({"title": current_sec, "data": current_data})
                    current_sec = line
                    current_data = {}
                    continue

                if ":" in line and current_sec:
                    key, val = [x.strip() for x in line.split(':', 1)]
                    sec_type = current_sec.lower().split()[0] # ex: extrait "audio" de "Audio #1"
                    
                    if sec_type in TARGET_KEYS and key in TARGET_KEYS[sec_type]:
                        current_data[key] = val
            
            # Ajout de la dernière section
            if current_sec and current_data: 
                sections_data.append({"title": current_sec, "data": current_data})
        except Exception as e:
            sections_data.append({"title": "Erreur", "data": {"Message": f"Impossible d'exécuter mediainfo: {e}"}})
            
        self.info_ready.emit(sections_data)

# --- UI COMPONENTS ---
class InfoCard(QFrame):
    def __init__(self, title, data_dict):
        super().__init__()
        color = "#7aa2f7" # Bleu Général
        if "Video" in title: color = "#bb9af7" # Violet
        elif "Audio" in title: color = "#9ece6a" # Vert
        elif "Text" in title: color = "#e0af68" # Orange

        self.setStyleSheet("""
            QFrame { background-color: #24283b; border-radius: 8px; border: 1px solid #414868; margin-bottom: 12px; }
            QLabel { border: none; }
        """)
        
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

class MovieMasterPro(QMainWindow):
    def __init__(self):
        super().__init__()
        self.tree_items = {} 
        self.full_data = defaultdict(list)
        self.scanned_count = 0
        
        self.setWindowTitle("Movie Master Pro - Champion Edition")
        self.resize(1400, 900)
        self.apply_styles()
        self.init_ui()
        
        self.target_dir = QFileDialog.getExistingDirectory(self, "Choisir le dossier de films")
        if self.target_dir:
            self.start_scan()
        else:
            sys.exit(0)

    def apply_styles(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #1a1b26; color: #c0caf5; font-family: 'Segoe UI', 'Inter', sans-serif; }
            
            QTreeWidget { background-color: #1f2335; border: 1px solid #292e42; border-radius: 8px; outline: none; }
            QTreeWidget::item { height: 38px; border-bottom: 1px solid #24283b; }
            QTreeWidget::item:selected { background-color: #3d59a1; color: #ffffff; border-radius: 4px; }
            
            QHeaderView::section { background-color: #1f2335; color: #7aa2f7; padding: 10px; border: none; font-weight: bold; font-size: 11px; text-transform: uppercase;}
            
            QLineEdit { background-color: #24283b; border: 1px solid #414868; border-radius: 6px; padding: 10px; color: #c0caf5; font-size: 13px; }
            QLineEdit:focus { border: 1px solid #7aa2f7; }
            
            QPushButton#btn_delete { 
                background-color: #f7768e; color: #1a1b26; border-radius: 4px; 
                font-size: 10px; font-weight: bold; height: 22px; max-width: 80px; 
            }
            QPushButton#btn_delete:hover { background-color: #ff9eaf; }
            
            QPushButton#btn_refresh { background-color: #24283b; border: 1px solid #414868; color: #7aa2f7; border-radius: 6px; padding: 8px 15px; font-weight: bold;}
            QPushButton#btn_refresh:hover { background-color: #414868; }
            
            QPushButton#btn_browse { 
                background-color: #24283b; 
                border: 1px solid #bb9af7; 
                color: #bb9af7; 
                border-radius: 6px; 
                padding: 8px 15px; 
                font-weight: bold;
                }
            QPushButton#btn_browse:hover { background-color: #bb9af7; color: #1a1b26; }
            
            QScrollArea { border: none; background-color: transparent; }
            QScrollBar:vertical { background: #1a1b26; width: 12px; margin: 0px; }
            QScrollBar::handle:vertical { background: #414868; border-radius: 6px; min-height: 20px; }
        """)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        
        # LE LAYOUT PRINCIPAL
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # 1. HEADER (Top Bar)
        top = QHBoxLayout()
        title = QLabel("MOVIE MASTER")
        title.setStyleSheet("font-weight: 950; font-size: 24px; color: #bb9af7; letter-spacing: -1px;")
        
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 Rechercher un film en direct...")
        self.search.setFixedWidth(400)
        self.search.textChanged.connect(self.filter_tree)

        self.btn_refresh = QPushButton("🔄 Actualiser")
        self.btn_refresh.setObjectName("btn_refresh")
        self.btn_refresh.clicked.connect(self.start_scan)

        self.btn_browse = QPushButton("Ouvrir un autre📂 Dossier")
        self.btn_browse.setObjectName("btn_browse") # Pour le style
        self.btn_browse.clicked.connect(self.change_directory)
        
        top.addWidget(title)
        top.addStretch() # Pousse la barre de recherche à droite
        top.addWidget(self.search)
        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_browse)
        layout.addLayout(top)

        # 2. STATUS
        self.lbl_status = QLabel("Prêt")
        self.lbl_status.setStyleSheet("color: #9ece6a; font-weight: bold; font-size: 12px;")
        layout.addWidget(self.lbl_status)

        # 3. SPLITTER (Le cœur du problème réglé ici)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding) # IMPORTANT
        
        # Panneau Gauche : Arbre
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Nom / Fichier", "Taille", "Action"])
        self.tree.setColumnWidth(0, 550)
        self.tree.setColumnWidth(1, 100)
        self.tree.setColumnWidth(2, 100)
        self.tree.itemSelectionChanged.connect(self.load_info)
        splitter.addWidget(self.tree)

        # Panneau Droit : Détails avec ScrollArea
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.details_container = QWidget()
        self.details_layout = QVBoxLayout(self.details_container)
        self.details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        # Message par défaut
        self.placeholder = QLabel("Sélectionnez un fichier pour voir les détails")
        self.placeholder.setStyleSheet("color: #565f89; font-style: italic; font-size: 14px;")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details_layout.addWidget(self.placeholder)
        
        self.scroll.setWidget(self.details_container)
        splitter.addWidget(self.scroll)

        # Proportions horizontales
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        
        # AJOUT DU SPLITTER AU LAYOUT AVEC STRETCH=1 (C'est ça qui empêche l'écrasement !)
        layout.addWidget(splitter, 1)

    def start_scan(self):
        self.tree.clear()
        self.tree_items.clear()
        self.full_data.clear()
        self.scanned_count = 0
        
        self.lbl_status.setText(f"⏳ Analyse du dossier en cours... ({self.target_dir})")
        
        self.scanner = ScannerThread(self.target_dir)
        self.scanner.file_found.connect(self.add_to_ui) # Connexion pour le temps réel
        self.scanner.finished.connect(self.on_scan_finished)
        self.scanner.start()

    def on_scan_finished(self):
        self.lbl_status.setText(f"✅ Analyse terminée : {len(self.full_data)} titres en mémoire.")
        self.sort_tree() # Déclenche le tri automatique

    def sort_tree(self):
        """ Trie la liste par doublons puis titre, et restaure les boutons supprimer """
        self.tree.setUpdatesEnabled(False)
        
        # 1. Extraire les parents
        items = []
        while self.tree.topLevelItemCount() > 0:
            items.append(self.tree.takeTopLevelItem(0))
        
        # 2. Logique de tri
        def get_sort_key(item):
            text = item.text(0)
            match = re.search(r'\((\d+)\)$', text)
            count = int(match.group(1)) if match else 0
            title = text.rsplit(' (', 1)[0].lower()
            return (-count, title)
            
        items.sort(key=get_sort_key)
        
        # 3. Ré-insertion et restauration des boutons
        for parent in items:
            self.tree.addTopLevelItem(parent)
            # Pour chaque fichier (enfant) sous ce parent
            for i in range(parent.childCount()):
                child = parent.child(i)
                path = child.data(0, Qt.ItemDataRole.UserRole)
                
                # On recrée le bouton car il a été détruit lors du takeTopLevelItem
                del_btn = QPushButton("Supprimer")
                del_btn.setObjectName("btn_delete")
                del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                del_btn.clicked.connect(lambda checked, p=path, c=child: self.confirm_delete(p, c))
                
                container = QWidget()
                l = QHBoxLayout(container)
                l.setContentsMargins(0, 0, 10, 0)
                l.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                l.addWidget(del_btn)
                self.tree.setItemWidget(child, 2, container)
        
        self.tree.setUpdatesEnabled(True)

    def add_to_ui(self, title, path):
        # 1. Mise à jour de la mémoire interne
        self.full_data[title].append(path)
        count = len(self.full_data[title])
        self.scanned_count += 1
        
        # Update du status text en direct
        if self.scanned_count % 5 == 0: # Rafraichit le texte tous les 5 fichiers pour la perf
            self.lbl_status.setText(f"⏳ Analyse en cours... ({self.scanned_count} fichiers trouvés)")

        # 2. Ajout dans l'arbre (Parent)
        if title not in self.tree_items:
            parent = QTreeWidgetItem(self.tree, [f"{title} ({count})", "", ""])
            parent.setFont(0, QFont("Segoe UI", 11, QFont.Weight.Bold))
            self.tree_items[title] = parent
            
            # Vérifie si le parent doit être caché à cause d'une recherche en cours
            current_search = self.search.text().lower()
            if current_search and current_search not in title.lower():
                parent.setHidden(True)
        else:
            parent = self.tree_items[title]
            parent.setText(0, f"{title} ({count})")
            if count > 1:
                parent.setForeground(0, QColor("#e0af68")) # Orange si doublon

        # 3. Ajout du fichier (Enfant)
        child = QTreeWidgetItem(parent, [os.path.basename(path), get_file_size(path), ""])
        child.setData(0, Qt.ItemDataRole.UserRole, path)
        
        # 4. Bouton Supprimer Stylisé
        del_btn = QPushButton("Supprimer")
        del_btn.setObjectName("btn_delete")
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.clicked.connect(lambda: self.confirm_delete(path, child))
        
        container = QWidget()
        l = QHBoxLayout(container)
        l.setContentsMargins(0, 0, 10, 0) # Marge droite pour ne pas coller
        l.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        l.addWidget(del_btn)
        self.tree.setItemWidget(child, 2, container)

    def change_directory(self):
        new_path = QFileDialog.getExistingDirectory(self, "Sélectionner un nouveau répertoire")
        if new_path and new_path != self.target_dir:
            self.target_dir = new_path
            self.start_scan() # Relance proprement le scan avec le nouveau chemin
            
    def filter_tree(self, text):
        query = text.lower()
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            should_show = query in parent.text(0).lower()
            parent.setHidden(not should_show)

    def load_info(self):
        sel = self.tree.selectedItems()
        if not sel or not sel[0].data(0, Qt.ItemDataRole.UserRole): return
        path = sel[0].data(0, Qt.ItemDataRole.UserRole)
        
        # Nettoyage du layout droit
        while self.details_layout.count():
            w = self.details_layout.takeAt(0).widget()
            if w: w.deleteLater()
            
        loader = QLabel("⚡ Extraction des métadonnées MediaInfo...")
        loader.setStyleSheet("color: #7aa2f7; font-weight: bold; font-size: 12px; margin-top: 20px;")
        loader.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details_layout.addWidget(loader)
        
        self.media_t = MediaInfoThread(path)
        self.media_t.info_ready.connect(self.draw_info)
        self.media_t.start()

    def draw_info(self, sections):
        while self.details_layout.count(): 
            w = self.details_layout.takeAt(0).widget()
            if w: w.deleteLater()
            
        if not sections:
            err = QLabel("Aucune donnée MediaInfo trouvée.")
            err.setStyleSheet("color: #f7768e;")
            self.details_layout.addWidget(err)
            
        for sec in sections:
            if sec["data"]: 
                self.details_layout.addWidget(InfoCard(sec["title"], sec["data"]))
                
        self.details_layout.addStretch() # Pousse joliment les cartes vers le haut

    def confirm_delete(self, path, item):
        msg = QMessageBox(self)
        msg.setWindowTitle("Suppression")
        msg.setText(f"Voulez-vous vraiment supprimer définitivement ce fichier ?\n\n{os.path.basename(path)}")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        
        msg.setStyleSheet("QLabel{color: #c0caf5; min-width: 400px;} QPushButton{width: 80px;}")

        if msg.exec() == QMessageBox.StandardButton.Yes:
            try:
                os.remove(path)
                parent = item.parent()
                title = parent.text(0).rsplit(' (', 1)[0]
                self.full_data[title].remove(path)
                parent.removeChild(item)
                
                new_count = len(self.full_data[title])
                if new_count == 0:
                    self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(parent))
                    del self.tree_items[title]
                else:
                    parent.setText(0, f"{title} ({new_count})")
                    if new_count == 1: parent.setForeground(0, QColor("#c0caf5"))
                    
                # Si le fichier supprimé était celui sélectionné, on vide les infos
                while self.details_layout.count(): 
                    w = self.details_layout.takeAt(0).widget()
                    if w: w.deleteLater()
                self.details_layout.addWidget(self.placeholder)
                
            except Exception as e:
                QMessageBox.critical(self, "Erreur", f"Erreur de suppression :\n{e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MovieMasterPro()
    win.show()
    sys.exit(app.exec())
