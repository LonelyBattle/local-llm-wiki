import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const PORT = Number(process.env.PORT || 3210);
const DATA_DIR = process.env.WIKI_DATA_DIR || 'D:\\LocalLLMWiki\\data';
const OLLAMA_URL = (process.env.OLLAMA_URL || 'http://127.0.0.1:11434').replace(/\/$/, '');
const PUBLIC_DIR = join(import.meta.dirname, 'public');
await mkdir(DATA_DIR, { recursive: true });

const db = new DatabaseSync(join(DATA_DIR, 'wiki.db'));
db.exec(`
  PRAGMA journal_mode=WAL;
  PRAGMA foreign_keys=ON;
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
  CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(title, content, tags, content='pages', content_rowid='id');
  CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid,title,content,tags) VALUES(new.id,new.title,new.content,new.tags);
  END;
  CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts,rowid,title,content,tags) VALUES('delete',old.id,old.title,old.content,old.tags);
  END;
  CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts,rowid,title,content,tags) VALUES('delete',old.id,old.title,old.content,old.tags);
    INSERT INTO pages_fts(rowid,title,content,tags) VALUES(new.id,new.title,new.content,new.tags);
  END;
`);

if (!db.prepare('SELECT id FROM pages LIMIT 1').get()) {
  const now = new Date().toISOString();
  db.prepare('INSERT INTO pages(title,slug,content,tags,created_at,updated_at) VALUES(?,?,?,?,?,?)').run(
    '欢迎使用本地知识库', 'welcome',
    '# 欢迎使用本地知识库\n\n这里是你的本地 Wiki。所有内容都保存在 **D:\\\\LocalLLMWiki\\\\data**。\n\n## 快速开始\n\n- 点击“新建页面”记录知识\n- 使用 Markdown 排版\n- 在右侧向本地模型提问，模型会优先参考 Wiki 内容\n- 按 `Ctrl + K` 快速搜索',
    '开始,说明', now, now
  );
}

const json = (res, status, data) => {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(data));
};
const body = async req => {
  let raw = '';
  for await (const chunk of req) { raw += chunk; if (raw.length > 2_000_000) throw new Error('内容过大'); }
  return raw ? JSON.parse(raw) : {};
};
const slugify = s => (s || 'untitled').trim().toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '') || `page-${Date.now()}`;
const pageRow = row => row ? { ...row, tags: row.tags ? row.tags.split(',').map(x => x.trim()).filter(Boolean) : [] } : null;
const listPages = () => db.prepare('SELECT id,title,slug,tags,updated_at FROM pages ORDER BY updated_at DESC').all().map(pageRow);

async function api(req, res, url) {
  if (req.method === 'GET' && url.pathname === '/api/pages') return json(res, 200, listPages());
  if (req.method === 'GET' && url.pathname.startsWith('/api/pages/')) {
    const id = Number(url.pathname.split('/').pop());
    const row = pageRow(db.prepare('SELECT * FROM pages WHERE id=?').get(id));
    return row ? json(res, 200, row) : json(res, 404, { error: '页面不存在' });
  }
  if (req.method === 'POST' && url.pathname === '/api/pages') {
    const data = await body(req); const title = String(data.title || '未命名页面').trim(); const now = new Date().toISOString();
    let slug = slugify(title), n = 2;
    while (db.prepare('SELECT 1 FROM pages WHERE slug=?').get(slug)) slug = `${slugify(title)}-${n++}`;
    const result = db.prepare('INSERT INTO pages(title,slug,content,tags,created_at,updated_at) VALUES(?,?,?,?,?,?)').run(title, slug, String(data.content || ''), (data.tags || []).join(','), now, now);
    return json(res, 201, pageRow(db.prepare('SELECT * FROM pages WHERE id=?').get(result.lastInsertRowid)));
  }
  if (req.method === 'PUT' && url.pathname.startsWith('/api/pages/')) {
    const id = Number(url.pathname.split('/').pop()); const old = db.prepare('SELECT * FROM pages WHERE id=?').get(id);
    if (!old) return json(res, 404, { error: '页面不存在' });
    const data = await body(req); const now = new Date().toISOString();
    db.prepare('INSERT INTO revisions(page_id,title,content,tags,saved_at) VALUES(?,?,?,?,?)').run(id, old.title, old.content, old.tags, now);
    db.prepare('UPDATE pages SET title=?,content=?,tags=?,updated_at=? WHERE id=?').run(String(data.title || old.title).trim(), String(data.content ?? old.content), (data.tags || []).join(','), now, id);
    return json(res, 200, pageRow(db.prepare('SELECT * FROM pages WHERE id=?').get(id)));
  }
  if (req.method === 'DELETE' && url.pathname.startsWith('/api/pages/')) {
    const id = Number(url.pathname.split('/').pop()); db.prepare('DELETE FROM pages WHERE id=?').run(id); return json(res, 200, { ok: true });
  }
  if (req.method === 'GET' && url.pathname === '/api/search') {
    const q = (url.searchParams.get('q') || '').trim();
    if (!q) return json(res, 200, listPages());
    let rows;
    try { rows = db.prepare("SELECT p.id,p.title,p.slug,p.tags,p.updated_at,snippet(pages_fts,1,'<mark>','</mark>','…',18) snippet FROM pages_fts JOIN pages p ON p.id=pages_fts.rowid WHERE pages_fts MATCH ? ORDER BY rank LIMIT 30").all(q.replace(/["']/g, ' ')); } catch { rows = []; }
    if (!rows.length) rows = db.prepare("SELECT id,title,slug,tags,updated_at,substr(content,1,180) snippet FROM pages WHERE title LIKE ? OR content LIKE ? OR tags LIKE ? LIMIT 30").all(`%${q}%`, `%${q}%`, `%${q}%`);
    return json(res, 200, rows.map(pageRow));
  }
  if (req.method === 'GET' && url.pathname === '/api/ollama/models') {
    try { const r = await fetch(`${OLLAMA_URL}/api/tags`, { signal: AbortSignal.timeout(2500) }); const d = await r.json(); return json(res, 200, { online: true, models: (d.models || []).map(x => x.name) }); }
    catch { return json(res, 200, { online: false, models: [] }); }
  }
  if (req.method === 'POST' && url.pathname === '/api/chat') {
    const data = await body(req); const question = String(data.question || '').trim();
    if (!question) return json(res, 400, { error: '请输入问题' });
    const terms = question.replace(/[^\p{L}\p{N}\s]/gu, ' ').split(/\s+/).filter(x => x.length > 1).slice(0, 8);
    let docs = [];
    try { docs = terms.length ? db.prepare('SELECT p.title,p.content FROM pages_fts JOIN pages p ON p.id=pages_fts.rowid WHERE pages_fts MATCH ? LIMIT 6').all(terms.join(' OR ')) : []; } catch {}
    if (!docs.length && terms.length) docs = db.prepare('SELECT title,content FROM pages WHERE ' + terms.map(() => '(title LIKE ? OR content LIKE ?)').join(' OR ') + ' ORDER BY updated_at DESC LIMIT 6').all(...terms.flatMap(x => [`%${x}%`, `%${x}%`]));
    if (!docs.length) docs = db.prepare('SELECT title,content FROM pages ORDER BY updated_at DESC LIMIT 5').all();
    const context = docs.map((d, i) => `[资料${i + 1}：${d.title}]\n${d.content.slice(0, 5000)}`).join('\n\n');
    try {
      const r = await fetch(`${OLLAMA_URL}/api/chat`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: data.model, stream: false, messages: [
        { role: 'system', content: '你是本地 Wiki 助手。优先依据提供的资料回答；资料不足时明确说明。回答使用简洁中文，并在末尾列出引用的资料标题。' },
        { role: 'user', content: `Wiki 资料：\n${context}\n\n问题：${question}` }
      ]}), signal: AbortSignal.timeout(120000) });
      if (!r.ok) throw new Error(await r.text()); const d = await r.json(); return json(res, 200, { answer: d.message?.content || '', sources: docs.map(x => x.title) });
    } catch (e) { return json(res, 503, { error: `无法连接本地模型：${e.message}` }); }
  }
  return json(res, 404, { error: '接口不存在' });
}

const mime = { '.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.svg':'image/svg+xml' };
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host}`);
    if (url.pathname.startsWith('/api/')) return await api(req, res, url);
    const rel = url.pathname === '/' ? 'index.html' : normalize(url.pathname).replace(/^[/\\]+/, '');
    const file = join(PUBLIC_DIR, rel); if (!file.startsWith(PUBLIC_DIR)) throw new Error('非法路径');
    const data = await readFile(file); res.writeHead(200, { 'content-type': mime[extname(file)] || 'application/octet-stream' }); res.end(data);
  } catch (e) { if (!res.headersSent) json(res, e.code === 'ENOENT' ? 404 : 500, { error: e.message }); }
});
server.listen(PORT, '127.0.0.1', () => console.log(`Local LLM Wiki: http://127.0.0.1:${PORT}\nData: ${DATA_DIR}`));
