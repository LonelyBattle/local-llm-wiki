import json
import mimetypes
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "3210"))
DATA_DIR = Path(os.environ.get("WIKI_DATA_DIR", r"D:\LocalLLMWiki\data"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
PUBLIC_DIR = Path(__file__).parent / "public"
DB_PATH = DATA_DIR / "wiki.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    with connect() as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS pages (
          id INTEGER PRIMARY KEY, title TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
          content TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revisions (
          id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL, title TEXT NOT NULL,
          content TEXT NOT NULL, tags TEXT NOT NULL, saved_at TEXT NOT NULL,
          FOREIGN KEY(page_id) REFERENCES pages(id) ON DELETE CASCADE
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
          title, content, tags, content='pages', content_rowid='id'
        );
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
          INSERT INTO pages_fts(rowid,title,content,tags) VALUES(new.id,new.title,new.content,new.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
          INSERT INTO pages_fts(pages_fts,rowid,title,content,tags)
          VALUES('delete',old.id,old.title,old.content,old.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
          INSERT INTO pages_fts(pages_fts,rowid,title,content,tags)
          VALUES('delete',old.id,old.title,old.content,old.tags);
          INSERT INTO pages_fts(rowid,title,content,tags) VALUES(new.id,new.title,new.content,new.tags);
        END;
        """)
        if not db.execute("SELECT id FROM pages LIMIT 1").fetchone():
            stamp = now()
            db.execute(
                "INSERT INTO pages(title,slug,content,tags,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("欢迎使用本地知识库", "welcome",
                 "# 欢迎使用本地知识库\n\n这里是你的本地 Wiki。所有内容都保存在 **D:\\\\LocalLLMWiki\\\\data**。\n\n## 快速开始\n\n- 点击“新建页面”记录知识\n- 使用 Markdown 排版\n- 在右侧向本地模型提问，模型会优先参考 Wiki 内容\n- 按 `Ctrl + K` 快速搜索",
                 "开始,说明", stamp, stamp),
            )


def page_dict(row):
    if not row:
        return None
    item = dict(row)
    item["tags"] = [tag.strip() for tag in item.get("tags", "").split(",") if tag.strip()]
    return item


def slugify(value):
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "-", value.strip().lower()).strip("-")
    return value or f"page-{int(datetime.now().timestamp() * 1000)}"


def ollama(path, payload=None, timeout=5):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{OLLAMA_URL}{path}", data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class WikiHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("内容过大")
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/pages":
                with connect() as db:
                    rows = db.execute("SELECT id,title,slug,tags,updated_at FROM pages ORDER BY updated_at DESC").fetchall()
                return self.send_json(200, [page_dict(row) for row in rows])
            match = re.fullmatch(r"/api/pages/(\d+)", parsed.path)
            if match:
                with connect() as db:
                    row = db.execute("SELECT * FROM pages WHERE id=?", (int(match.group(1)),)).fetchone()
                return self.send_json(200, page_dict(row)) if row else self.send_json(404, {"error": "页面不存在"})
            if parsed.path == "/api/search":
                return self.search(parse_qs(parsed.query).get("q", [""])[0].strip())
            if parsed.path == "/api/ollama/models":
                try:
                    data = ollama("/api/tags", timeout=2.5)
                    return self.send_json(200, {"online": True, "models": [m["name"] for m in data.get("models", [])]})
                except Exception:
                    return self.send_json(200, {"online": False, "models": []})
            if parsed.path.startswith("/api/"):
                return self.send_json(404, {"error": "接口不存在"})
            self.path = "/index.html" if parsed.path == "/" else unquote(parsed.path)
            return super().do_GET()
        except Exception as exc:
            return self.send_json(500, {"error": str(exc)})

    def search(self, query):
        with connect() as db:
            if not query:
                rows = db.execute("SELECT id,title,slug,tags,updated_at FROM pages ORDER BY updated_at DESC").fetchall()
            else:
                try:
                    rows = db.execute("""SELECT p.id,p.title,p.slug,p.tags,p.updated_at,
                      snippet(pages_fts,1,'<mark>','</mark>','…',18) snippet
                      FROM pages_fts JOIN pages p ON p.id=pages_fts.rowid
                      WHERE pages_fts MATCH ? ORDER BY rank LIMIT 30""", (re.sub(r'["\']', ' ', query),)).fetchall()
                except sqlite3.Error:
                    rows = []
                if not rows:
                    like = f"%{query}%"
                    rows = db.execute("""SELECT id,title,slug,tags,updated_at,substr(content,1,180) snippet
                      FROM pages WHERE title LIKE ? OR content LIKE ? OR tags LIKE ? LIMIT 30""", (like, like, like)).fetchall()
        self.send_json(200, [page_dict(row) for row in rows])

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data = self.read_json()
            if parsed.path == "/api/pages":
                title = str(data.get("title") or "未命名页面").strip()
                base, slug, suffix = slugify(title), slugify(title), 2
                with connect() as db:
                    while db.execute("SELECT 1 FROM pages WHERE slug=?", (slug,)).fetchone():
                        slug, suffix = f"{base}-{suffix}", suffix + 1
                    stamp = now()
                    cur = db.execute("INSERT INTO pages(title,slug,content,tags,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (title, slug, str(data.get("content", "")), ",".join(data.get("tags", [])), stamp, stamp))
                    row = db.execute("SELECT * FROM pages WHERE id=?", (cur.lastrowid,)).fetchone()
                return self.send_json(201, page_dict(row))
            if parsed.path == "/api/chat":
                return self.chat(data)
            return self.send_json(404, {"error": "接口不存在"})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            return self.send_json(500, {"error": str(exc)})

    def chat(self, data):
        question = str(data.get("question", "")).strip()
        if not question:
            return self.send_json(400, {"error": "请输入问题"})
        terms = [x for x in re.sub(r"[^\w\u4e00-\u9fff\s]", " ", question).split() if len(x) > 1][:8]
        docs = []
        with connect() as db:
            if terms:
                try:
                    docs = db.execute("""SELECT p.title,p.content FROM pages_fts
                      JOIN pages p ON p.id=pages_fts.rowid WHERE pages_fts MATCH ? LIMIT 6""",
                      (" OR ".join(terms),)).fetchall()
                except sqlite3.Error:
                    pass
            if not docs and terms:
                clauses = " OR ".join("(title LIKE ? OR content LIKE ?)" for _ in terms)
                args = [value for term in terms for value in (f"%{term}%", f"%{term}%")]
                docs = db.execute(f"SELECT title,content FROM pages WHERE {clauses} ORDER BY updated_at DESC LIMIT 6", args).fetchall()
            if not docs:
                docs = db.execute("SELECT title,content FROM pages ORDER BY updated_at DESC LIMIT 5").fetchall()
        context = "\n\n".join(f"[资料{i + 1}：{row['title']}]\n{row['content'][:5000]}" for i, row in enumerate(docs))
        try:
            result = ollama("/api/chat", {"model": data.get("model"), "stream": False, "messages": [
                {"role": "system", "content": "你是本地 Wiki 助手。优先依据提供的资料回答；资料不足时明确说明。回答使用简洁中文，并在末尾列出引用的资料标题。"},
                {"role": "user", "content": f"Wiki 资料：\n{context}\n\n问题：{question}"},
            ]}, timeout=120)
            return self.send_json(200, {"answer": result.get("message", {}).get("content", ""), "sources": [row["title"] for row in docs]})
        except Exception as exc:
            return self.send_json(503, {"error": f"无法连接本地模型：{exc}"})

    def do_PUT(self):
        match = re.fullmatch(r"/api/pages/(\d+)", urlparse(self.path).path)
        if not match:
            return self.send_json(404, {"error": "接口不存在"})
        try:
            page_id, data = int(match.group(1)), self.read_json()
            with connect() as db:
                old = db.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
                if not old:
                    return self.send_json(404, {"error": "页面不存在"})
                stamp = now()
                db.execute("INSERT INTO revisions(page_id,title,content,tags,saved_at) VALUES(?,?,?,?,?)",
                    (page_id, old["title"], old["content"], old["tags"], stamp))
                db.execute("UPDATE pages SET title=?,content=?,tags=?,updated_at=? WHERE id=?",
                    (str(data.get("title") or old["title"]).strip(), str(data.get("content", old["content"])), ",".join(data.get("tags", [])), stamp, page_id))
                row = db.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
            return self.send_json(200, page_dict(row))
        except Exception as exc:
            return self.send_json(400, {"error": str(exc)})

    def do_DELETE(self):
        match = re.fullmatch(r"/api/pages/(\d+)", urlparse(self.path).path)
        if not match:
            return self.send_json(404, {"error": "接口不存在"})
        with connect() as db:
            db.execute("DELETE FROM pages WHERE id=?", (int(match.group(1)),))
        return self.send_json(200, {"ok": True})


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), WikiHandler)
    print(f"Local LLM Wiki: http://{HOST}:{PORT}\nData: {DATA_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
