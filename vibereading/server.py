"""Local-only GUI server, file-save refresh and versioned explanation storage."""

import copy
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

from .analyzer import SUPPORTED_LANGUAGES, EXTENSIONS, IGNORED_DIRS, MAX_FILES, analyze_project
from .storage import AnnotationStore, ConflictError

APP_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = Path(__file__).resolve().parent / "web"


def fingerprint(root: Path) -> tuple:
    """Detect saved-file changes without evaluating source or following links."""
    items = []
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = sorted(name for name in folders if name not in IGNORED_DIRS
                            and not (Path(directory) / name).is_symlink()
                            and not (Path(directory) / name).is_junction())
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() not in EXTENSIONS or path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            try:
                stat = path.stat()
                items.append((str(path.relative_to(root)), stat.st_mtime_ns, stat.st_size))
            except OSError:
                items.append((str(path), "unavailable", 0))
            if len(items) >= MAX_FILES:
                return tuple(items)
    return tuple(items)


class ProjectSession:
    """Serialize scans and preserve annotation revisions independently of the graph."""

    def __init__(self, root: Path, database: Path | None = None, interval: float = 1.0):
        self.lock = threading.RLock()
        self.scan_lock = threading.RLock()
        self.stop = threading.Event()
        self.interval = interval
        self.store = AnnotationStore(database or APP_ROOT / ".vibereading" / "state.sqlite3")
        self.root = root.resolve()
        self.revision = 0
        self.last_scan = ""
        self.error = None
        self.graph = {"nodes": [], "edges": [], "files": [], "diagnostics": [], "unresolved": [], "stats": {}}
        self.saved_fingerprint = None
        self.open_project(root)
        self.thread = threading.Thread(target=self.watch, daemon=True, name="saved-file-watcher")
        self.thread.start()

    def open_project(self, root: Path):
        root = root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError("请选择一个存在的项目文件夹")
        with self.scan_lock:
            before = fingerprint(root)
            graph = analyze_project(root)
            with self.lock:
                self.root = root
                self.graph = graph
                self.saved_fingerprint = before
                self.last_scan = datetime.now(timezone.utc).isoformat()
                self.error = None
                self.revision += 1
        return self.snapshot()

    def watch(self):
        """A short quiet period groups atomic editor-save operations."""
        while not self.stop.wait(self.interval):
            try:
                with self.scan_lock:
                    root = self.root
                    observed = fingerprint(root)
                    if observed != self.saved_fingerprint:
                        if self.stop.wait(0.2):
                            return
                        self.open_project(root)
            except Exception as error:
                # Retain the last graph rather than silently replace it on I/O failure.
                with self.lock:
                    message = f"刷新失败，保留上次图谱：{error}"
                    if self.error != message:
                        self.error = message
                        self.revision += 1

    def status(self):
        with self.lock:
            return {"project": {"root": str(self.root), "name": self.root.name},
                    "application": "VibeReading", "version": "0.1.0",
                    "revision": self.revision, "watching": not self.stop.is_set(),
                    "last_scan": self.last_scan, "error": self.error}

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.graph)
            result.update(self.status(), languages=list(SUPPORTED_LANGUAGES))
            annotations = self.store.latest(str(self.root))
            for node in result["nodes"]:
                annotation = annotations.get(node["id"])
                if annotation and annotation["code_hash"] != node["code_hash"]:
                    annotation = dict(annotation, status="stale")
                node["annotation"] = annotation
            return result

    def save_annotation(self, data: dict):
        # Use the same scan lock for project switching, source hashing and writes.
        with self.scan_lock, self.lock:
            self.check_project(data.get("project_root"))
            if fingerprint(self.root) != self.saved_fingerprint:
                self.open_project(self.root)
            node = next((n for n in self.graph["nodes"] if n["id"] == data.get("id")), None)
            if node is None:
                raise ConflictError("函数已不存在，请重新读取图谱")
            if data.get("code_hash") != node["code_hash"]:
                raise ConflictError("源码已变化，请先核对新版代码再保存说明")
            expected = data.get("expected_version", 0)
            if not isinstance(expected, int) or expected < 0:
                raise ValueError("说明版本无效")
            annotation = self.store.save(str(self.root), node["id"], node["code_hash"], expected, data)
            self.revision += 1
            return {"annotation": annotation, "revision": self.revision}

    def check_project(self, project_root):
        """Prevent identical symbols in another open project receiving a stale write."""
        if not isinstance(project_root, str) or os.path.normcase(str(Path(project_root).resolve())) != os.path.normcase(str(self.root)):
            raise ConflictError("当前项目已变化，请回到草稿所属项目后再保存")

    def import_annotations(self, items: list, project_root: str):
        # Keep a whole import bound to one project even if another tab switches.
        with self.scan_lock, self.lock:
            self.check_project(project_root)
            return self._import_annotations(items)

    def _import_annotations(self, items: list):
        """Imported AI explanations always enter as drafts, even if marked approved."""
        if not isinstance(items, list) or len(items) > 2000:
            raise ValueError("items 必须是最多 2000 条记录的数组")
        imported, skipped = 0, []
        for item in items:
            if not isinstance(item, dict):
                skipped.append({"id": None, "reason": "不是对象"})
                continue
            with self.scan_lock, self.lock:
                node = next((n for n in self.graph["nodes"] if n["id"] == item.get("id")), None)
                existing = self.store.latest(str(self.root))
                if not node or node["id"] in existing:
                    skipped.append({"id": item.get("id"), "reason": "函数不存在或已有人工/历史说明"})
                    continue
                data = dict(item, project_root=str(self.root), code_hash=node["code_hash"], expected_version=0, origin="ai", status="draft")
                try:
                    self.save_annotation(data)
                    imported += 1
                except ValueError as error:
                    skipped.append({"id": item.get("id"), "reason": str(error)})
        return {"imported": imported, "skipped": skipped, "revision": self.revision}

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, session):
        self.session = session
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    """Serve trusted app assets and reject web pages making cross-origin writes."""

    def log_message(self, format, *args):
        # Polling does not flood the launch terminal or expose project paths.
        if args and str(args[1] if len(args) > 1 else "") not in ("200", "304"):
            super().log_message(format, *args)

    def allowed(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        return True

    def send_body(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status=200):
        self.send_body(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self):
        if not self.allowed():
            return self.json({"error": "仅允许本机同源访问"}, 403)
        route = urlparse(self.path).path
        try:
            if route == "/api/graph":
                return self.json(self.server.session.snapshot())
            if route == "/api/status":
                return self.json(self.server.session.status())
            if route == "/api/annotations/export":
                graph = self.server.session.snapshot()
                return self.json({"schema_version": 1, "project": graph["project"], "items": [
                    dict(node["annotation"], id=node["id"], file=node["file"], qualified_name=node["qualified_name"])
                    for node in graph["nodes"] if node.get("annotation")]})
            # Serve only fixed app assets; never map arbitrary paths into the project.
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/index.html": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if route not in assets:
                return self.json({"error": "未找到该资源"}, 404)
            filename, content_type = assets[route]
            self.send_body((WEB_ROOT / filename).read_bytes(), content_type)
        except Exception as error:
            self.json({"error": str(error)}, 500)

    def do_POST(self):
        if not self.allowed():
            return self.json({"error": "仅允许本机同源访问"}, 403)
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.json({"error": "请求必须使用 JSON"}, 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                raise ValueError("请求大小无效（上限 2 MB）")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求必须是对象")
            route = urlparse(self.path).path
            session = self.server.session
            if route == "/api/project":
                project = data.get("path")
                if not isinstance(project, str) or not project.strip():
                    raise ValueError("请填写项目文件夹路径")
                return self.json(session.open_project(Path(project.strip())))
            if route == "/api/annotations":
                return self.json(session.save_annotation(data))
            if route == "/api/annotations/import":
                return self.json(session.import_annotations(data.get("items"), data.get("project_root")))
            if route == "/api/pick-directory":
                # A native chooser opens only after the user clicks this action.
                try:
                    import tkinter as tk
                    from tkinter import filedialog
                    window = tk.Tk()
                    window.withdraw()
                    window.attributes("-topmost", True)
                    selected = filedialog.askdirectory(title="选择要阅读的代码目录", parent=window)
                    window.destroy()
                except Exception:
                    return self.json({"error": "本机目录选择器不可用，请在路径框粘贴目录路径"}, 503)
                return self.json({"path": selected, "cancelled": not bool(selected)})
            return self.json({"error": "未找到该操作"}, 404)
        except ConflictError as error:
            self.json({"error": str(error)}, 409)
        except (ValueError, OSError) as error:
            self.json({"error": str(error)}, 400)
        except Exception as error:
            self.json({"error": str(error)}, 500)


def serve(project: Path, port: int = 8871, open_browser: bool = False):
    """Bind only to loopback; the inspected source is never served as executable code."""
    session = ProjectSession(project)
    try:
        server = LocalServer(("127.0.0.1", port), session)
    except OSError:
        session.close()
        raise
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"VibeReading: {address}\nProject: {project.resolve()}\nStop: Ctrl+C", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(address)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        session.close()
