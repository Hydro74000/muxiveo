import os
import ast
import json
import fnmatch

class ProjectAnalyzer(ast.NodeVisitor):
    def __init__(self, file_path, project_root, local_files_index):
        self.project_root = os.path.abspath(project_root)
        self.file_path = os.path.relpath(file_path, self.project_root)
        self.local_files_index = local_files_index
        self.current_scope = None
        self.nodes = []
        self.links = []
        
        self.nodes.append({
            "id": self.file_path,
            "group": "File",
            "desc": f"Module Python : {self.file_path}"
        })

    def visit_Import(self, node):
        for alias in node.names:
            self._add_import_link(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            self._add_import_link(node.module)
        self.generic_visit(node)

    def _add_import_link(self, module_name):
        rel_path = module_name.replace('.', os.sep) + ".py"
        if rel_path in self.local_files_index:
            self.links.append({
                "source": self.file_path,
                "target": rel_path,
                "type": "dependency"
            })

    def visit_ClassDef(self, node):
        class_id = f"{self.file_path}::{node.name}"
        self.nodes.append({
            "id": class_id, "group": "Class",
            "desc": f"Classe définie dans {self.file_path}"
        })
        self.links.append({"source": self.file_path, "target": class_id, "type": "containment"})
        old_scope = self.current_scope
        self.current_scope = class_id
        self.generic_visit(node)
        self.current_scope = old_scope

    def visit_FunctionDef(self, node):
        func_id = f"{self.file_path}::{node.name}"
        if self.current_scope:
            func_id = f"{self.current_scope}.{node.name}"
        
        self.nodes.append({
            "id": func_id, "group": "Function",
            "desc": f"Fonction/Méthode : {node.name}"
        })
        parent = self.current_scope if self.current_scope else self.file_path
        self.links.append({"source": parent, "target": func_id, "type": "containment"})
        self.generic_visit(node)

def parse_gitignore(root_path):
    patterns = ['.git', '__pycache__', 'venv', '.venv', 'test', 'tests', 'test_*.py', '*_test.py', '*init*.py']
    gitignore_path = os.path.join(root_path, '.gitignore')
    if os.path.exists(gitignore_path):
        with open(gitignore_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'): patterns.append(line.rstrip('/'))
    return list(set(patterns))

def is_ignored(path, patterns, root_path):
    rel_path = os.path.relpath(path, root_path)
    if rel_path == ".": return False
    parts = rel_path.split(os.sep)
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern) or \
           fnmatch.fnmatch(os.path.basename(path), pattern) or \
           any(fnmatch.fnmatch(p, pattern) for p in parts):
            return True
    return False

def scan_project(path):
    path = os.path.abspath(path)
    ignore_patterns = parse_gitignore(path)
    local_files_index = set()
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d), ignore_patterns, path)]
        for file in files:
            full_path = os.path.join(root, file)
            if file.endswith(".py") and not is_ignored(full_path, ignore_patterns, path):
                local_files_index.add(os.path.relpath(full_path, path))

    all_nodes, all_links = [], []
    for rel_file in local_files_index:
        try:
            with open(os.path.join(path, rel_file), "r", encoding="utf-8-sig", errors="ignore") as f:
                tree = ast.parse(f.read())
                analyzer = ProjectAnalyzer(os.path.join(path, rel_file), path, local_files_index)
                analyzer.visit(tree)
                all_nodes.extend(analyzer.nodes)
                all_links.extend(analyzer.links)
        except: continue
    return {"nodes": list({n['id']: n for n in all_nodes}.values()), "links": all_links}

def generate_html(data):
    json_data = json.dumps(data, indent=4)
    html_template = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Constellation Adaptive</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
    <style>
        body {{ margin: 0; background: #0b0f1a; color: white; font-family: 'Segoe UI', sans-serif; overflow: hidden; display: flex; }}
        #graph-container {{ flex-grow: 1; height: 100vh; cursor: grab; }}
        #info-panel {{ width: 320px; background: rgba(15, 23, 42, 0.95); border-left: 1px solid #334155; padding: 25px; display: none; flex-direction: column; z-index: 100; box-shadow: -10px 0 30px rgba(0,0,0,0.5); }}
        .node {{ stroke: #fff; cursor: pointer; transition: stroke-width 0.2s; }}
        .link {{ stroke-opacity: 0.15; stroke: #475569; }}
        .link-dependency {{ stroke: #fbbf24; stroke-dasharray: 4; stroke-opacity: 0.5; }}
        .label {{ fill: #94a3b8; font-size: 11px; pointer-events: none; font-weight: 300; }}
        .highlight-link {{ stroke: #f472b6 !important; stroke-opacity: 1 !important; stroke-width: 2px !important; }}
        h2 {{ color: #f8fafc; font-size: 16px; margin: 0; word-break: break-all; }}
        .tag {{ background: #1e293b; padding: 4px 10px; border-radius: 6px; font-size: 11px; margin: 10px 0; display: inline-block; color: #38bdf8; border: 1px solid #334155; }}
    </style>
</head>
<body>
    <div id="graph-container"></div>
    <div id="info-panel">
        <div style="cursor:pointer; color: #64748b; text-align: right;" onclick="this.parentElement.style.display='none'">✕</div>
        <h2 id="info-title"></h2>
        <div id="info-meta" class="tag"></div>
        <p id="info-desc" style="color: #94a3b8; font-size: 14px; line-height: 1.6;"></p>
    </div>

    <script>
        const data = {json_data};
        const width = window.innerWidth, height = window.innerHeight;
        const svg = d3.select("#graph-container").append("svg").attr("width", "100%").attr("height", "100%");
        const g = svg.append("g");

        // --- COMPORTEMENT ZOOM ---
        const zoom = d3.zoom().scaleExtent([0.01, 8]).on("zoom", (e) => {{
            g.attr("transform", e.transform);
            
            // ADAPTATION TAILLE : Les fichiers grossissent quand on dézoome (e.transform.k petit)
            // On utilise une racine carrée pour que l'effet ne soit pas trop violent
            const scaleFactor = 1 / Math.sqrt(e.transform.k);
            
            node.attr("r", d => {{
                const base = d.group === "File" ? 12 : d.group === "Class" ? 7 : 4;
                return base * scaleFactor;
            }}).attr("stroke-width", 1 * scaleFactor);

            labels.style("font-size", (11 * scaleFactor) + "px")
                  .attr("dy", - (15 * scaleFactor));
        }});
        svg.call(zoom);

        const simulation = d3.forceSimulation(data.nodes)
            .force("link", d3.forceLink(data.links).id(d => d.id).distance(120))
            .force("charge", d3.forceManyBody().strength(-400))
            .force("center", d3.forceCenter(width / 2.5, height / 2))
            .force("x", d3.forceX(width / 2).strength(0.02))
            .force("y", d3.forceY(height / 2).strength(0.02));

        const link = g.append("g").selectAll("line").data(data.links).enter().append("line")
            .attr("class", d => d.type === 'dependency' ? "link link-dependency" : "link");

        const node = g.append("g").selectAll("circle").data(data.nodes).enter().append("circle")
            .attr("class", "node")
            .attr("r", d => d.group === "File" ? 12 : d.group === "Class" ? 7 : 4)
            .attr("fill", d => {{
                if(d.group === "File") return "#f59e0b";
                if(d.group === "Class") return "#38bdf8";
                return "#10b981";
            }})
            .call(d3.drag().on("start", dragstarted).on("drag", dragged).on("end", dragended));

        const labels = g.append("g").selectAll("text").data(data.nodes).enter().append("text")
            .attr("class", "label").text(d => d.id.split(/[\\\\/]|::/).pop()).attr("dy", -15).attr("text-anchor", "middle");

        // --- CLIC : RECENTRAGE ET ZOOM ---
        node.on("click", function(e, d) {{
            const targetScale = 1.2;
            const transform = d3.zoomIdentity
                .translate(window.innerWidth / 2.8, window.innerHeight / 2) // Centre vers la gauche (laisse place au panel)
                .scale(targetScale)
                .translate(-d.x, -d.y);

            svg.transition().duration(1000).ease(d3.easeCubicInOut).call(zoom.transform, transform);

            document.getElementById("info-panel").style.display = "flex";
            document.getElementById("info-title").innerText = d.id;
            document.getElementById("info-desc").innerText = d.desc;
            document.getElementById("info-meta").innerText = d.group;
            e.stopPropagation();
        }});

        node.on("mouseover", function(e, d) {{
            link.classed("highlight-link", l => l.source.id === d.id || l.target.id === d.id);
        }}).on("mouseout", function() {{
            link.classed("highlight-link", false);
        }});

        simulation.on("tick", () => {{
            link.attr("x1", d => d.source.x).attr("y1", d => d.source.y).attr("x2", d => d.target.x).attr("y2", d => d.target.y);
            node.attr("cx", d => d.x).attr("cy", d => d.y);
            labels.attr("x", d => d.x).attr("y", d => d.y);
        }});

        function dragstarted(e, d) {{ if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }}
        function dragged(e, d) {{ d.fx = e.x; d.fy = e.y; }}
        function dragended(e, d) {{ if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }}
    </script>
</body>
</html>
    """
    with open("project_map.html", "w", encoding="utf-8") as f:
        f.write(html_template)
    print("Succès : Les tests sont filtrés et la navigation est maintenant 'Smart Zoom'.")

if __name__ == "__main__":
    generate_html(scan_project('.'))
