"""ブラウザで動く編集画面。

`python3 midi2tab.py song.mid --edit` で 127.0.0.1 にサーバーを立て、譜面を表示する。
音やコードネームをクリックして直した内容は JSON (song.mid.edits.json) に保存され、
次回の編集やHTML/PDF出力にもそのまま反映される。
"""
from __future__ import annotations

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import marks as marklib
import midi2tab
import pdfout
import tabsvg

MAX_BODY = 2_000_000
DEFAULT_PORT = 8765


class Session:
    """1つのMIDIファイルの編集状態。"""

    def __init__(self, mid, fixed: dict, edits: dict, edits_path: str | None, default_title: str):
        self.mid = mid
        self.fixed = fixed
        self.edits = edits
        self.edits_path = edits_path
        self.default_title = default_title
        self.lock = threading.Lock()

    def title(self, edits: dict) -> str:
        return edits.get("title") or self.default_title

    def render(self, edits: dict | None = None) -> dict:
        edits = self.edits if edits is None else edits
        score = midi2tab.analyze_with_edits(self.mid, self.fixed, edits)
        options = {k: edits.get("options", {}).get(k, d)
                   for k, d in midi2tab.EDITABLE_OPTIONS.items()}
        return {"title": self.title(edits), "meta": tabsvg.meta_line(score),
                "svgs": tabsvg.render_systems(score), "warnings": score.warnings,
                "strings": [midi2tab.pitch_class_name(p) for p in score.opens],
                "options": options, "edits": edits}

    def update(self, edits: dict) -> dict:
        """新しい編集内容で描画できたら採用して保存する。失敗したら ValueError。"""
        if not isinstance(edits, dict):
            raise ValueError("編集内容の形式が不正です")
        with self.lock:
            try:
                result = self.render(edits)
            except (KeyError, TypeError, AttributeError) as e:
                raise ValueError(f"編集内容の形式が不正です: {e}") from None
            self.edits = edits
            if self.edits_path:
                tmp = self.edits_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(edits, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.edits_path)
            return result

    def pdf(self) -> bytes:
        with self.lock:
            score = midi2tab.analyze_with_edits(self.mid, self.fixed, self.edits)
            html = tabsvg.render_html(score, self.title(self.edits))
        return pdfout.html_to_pdf(html)


def make_handler(session: Session):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 標準のアクセスログは出さない
            pass

        def send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, data: dict):
            self.send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                      "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/state":
                try:
                    self.send_json(200, session.render())
                except ValueError as e:
                    self.send_json(400, {"error": str(e)})
            elif path == "/api/pdf":
                try:
                    data = session.pdf()
                except (pdfout.PdfError, ValueError) as e:
                    self.send_json(501, {"error": str(e)})
                    return
                self.send(200, data, "application/pdf",
                          {"Content-Disposition": 'attachment; filename="tab.pdf"'})
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            # 他サイトのページからの書き込みを拒否する
            origin = self.headers.get("Origin")
            if origin and origin.split("://")[-1] != self.headers.get("Host"):
                self.send_json(403, {"error": "forbidden"})
                return
            if self.path != "/api/edits":
                self.send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                self.send_json(400, {"error": "リクエストが大きすぎます"})
                return
            try:
                edits = json.loads(self.rfile.read(length))
                self.send_json(200, session.update(edits))
            except (ValueError, json.JSONDecodeError) as e:
                self.send_json(400, {"error": str(e)})

    return Handler


def make_server(session: Session, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(session))


def open_server(session: Session, port: int | None) -> ThreadingHTTPServer:
    """port 未指定なら 8765 を試し、使えなければ空いている番号を自動で選ぶ。

    Windows では Hyper-V などが 8765 付近を予約していて開けないことがある。
    """
    if port is not None:
        return make_server(session, port)
    try:
        return make_server(session, DEFAULT_PORT)
    except OSError:
        return make_server(session, 0)


def serve(mid, fixed: dict, edits: dict, edits_path: str, default_title: str,
          port: int | None = None, open_browser: bool = True) -> int:
    session = Session(mid, fixed, edits, edits_path, default_title)
    try:
        session.render()
    except ValueError as e:
        print(f"エラー: {e}")
        return 1
    try:
        server = open_server(session, port)
    except OSError as e:
        print(f"ポート {port} を開けません ({e})。--port で別の番号を指定してください")
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"編集画面: {url}  (終了は Ctrl+C)")
    print(f"編集内容の保存先: {edits_path}")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>midi2tab 編集</title>
<style>__PAGE_CSS__
  body { margin-top: 0; }
  .bar { position: sticky; top: 0; z-index: 5; background: #f6f6f4; border-bottom: 1px solid #ddd;
         margin: 0 -16px 20px; padding: 8px 16px; display: flex; flex-wrap: wrap; gap: 8px 14px;
         align-items: center; font-size: 13px; }
  .bar label { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
  .bar input[type=text], .bar input[type=number], .bar select {
         font: inherit; padding: 3px 5px; border: 1px solid #bbb; border-radius: 4px; background: #fff; }
  .bar input[type=number] { width: 48px; }
  button { font: inherit; padding: 4px 10px; border: 1px solid #aaa; border-radius: 4px;
           background: #fff; cursor: pointer; }
  button:hover:not(:disabled) { background: #eef3ff; border-color: #6b8de3; }
  button:disabled { opacity: .45; cursor: default; }
  button.primary { background: #2f5fd0; color: #fff; border-color: #2f5fd0; }
  button.primary:hover:not(:disabled) { background: #264fb0; }
  button.on { background: #dce6ff; border-color: #6b8de3; }
  .spacer { flex: 1; }
  #title { font-size: 22px; font-weight: 700; border: 1px solid transparent; border-radius: 4px;
           padding: 2px 4px; margin: 0 0 2px -5px; width: 100%; box-sizing: border-box; }
  #title:hover, #title:focus { border-color: #bbb; outline: none; }
  #msg { color: #a33; font-size: 13px; min-height: 18px; margin: 0 0 6px; }
  #score text.n, #score text.c { cursor: pointer; }
  #score text.n:hover, #score text.c:hover { fill: #2f5fd0; }
  #score text.sel { fill: #d0452f; }
  #pop { position: absolute; z-index: 10; background: #fff; border: 1px solid #999; border-radius: 8px;
         box-shadow: 0 6px 24px rgba(0,0,0,.18); padding: 10px 12px; font-size: 13px; width: 330px;
         max-height: 70vh; overflow-y: auto; }
  #pop button { padding: 3px 7px; font-size: 12px; }
  #pop select { font: inherit; padding: 3px 5px; }
  #pop h3 { font-size: 12px; color: #666; font-weight: 600; margin: 8px 0 4px; }
  #pop h3:first-child { margin-top: 0; }
  #pop .row { display: flex; flex-wrap: wrap; gap: 5px; }
  #pop input[type=text] { font: inherit; padding: 3px 5px; border: 1px solid #bbb; border-radius: 4px;
                          width: 110px; }
  #pop .danger { color: #a33; }
  .hint { color: #777; font-size: 12px; margin: 14px 0 30px; }
</style>
</head>
<body>
<div class="bar no-print">
  <label>チューニング
    <select id="tuning"></select></label>
  <label>カポ <input id="capo" type="number" min="0" max="12"></label>
  <label>グリッド
    <select id="grid">
      <option value="">自動</option><option value="8">8分</option><option value="16">16分</option>
      <option value="32">32分</option><option value="12">3連8分</option>
      <option value="24">3連16分</option><option value="48">16分+3連</option>
    </select></label>
  <label><input id="legato" type="checkbox"> H/P を推定</label>
  <label>タッピング
    <select id="tapping">
      <option value="">なし</option><option value="auto">易しくなる小節だけ</option>
      <option value="on">積極的に</option>
    </select></label>
  <label><input id="chords" type="checkbox"> コード名を推定</label>
  <label><input id="expression" type="checkbox"> チョーキング/ビブラートを推定</label>
  <span class="spacer"></span>
  <button id="undo">元に戻す</button>
  <button id="reset">手直しをリセット</button>
  <button id="pdf" class="primary">PDFで保存</button>
</div>
<input id="title" type="text" aria-label="タイトル">
<p class="meta" id="meta"></p>
<p id="msg" class="no-print"></p>
<div id="score"></div>
<p class="hint no-print">数字をクリック: 弦の変更・奏法記号(スライド、チョーキング、ビブラートなど)・コード名・音の削除。コード名をクリック: 名前の変更。変更は自動で保存されます。</p>
<div id="pop" class="no-print" hidden></div>
<script>
const TUNINGS = __TUNINGS__;
const CONNECTIONS = __CONNECTIONS__, MARKS = __MARKS__, BENDS = __BENDS__, EXCLUSIVE = __EXCLUSIVE__;
const isBend = c => /^(pbr|pb|br|b)[0-9.]+$/.test(c);
const $ = id => document.getElementById(id);
let edits = {}, history = [], strings = [];

function show(state) {
  edits = state.edits; strings = state.strings;
  $('title').value = state.title;
  document.title = state.title + ' - midi2tab';
  $('meta').textContent = state.meta;
  $('score').innerHTML = state.svgs.join('');
  $('msg').textContent = state.warnings.map(w => '注意: ' + w).join(' / ');
  const o = state.options;
  if (![...$('tuning').options].some(x => x.value === o.tuning))
    $('tuning').add(new Option(o.tuning, o.tuning));
  $('tuning').value = o.tuning;
  $('capo').value = o.capo;
  $('grid').value = o.grid == null ? '' : String(o.grid);
  $('legato').checked = o.legato || o.tapping;
  $('tapping').value = o.tapping === 'auto' ? 'auto' : o.tapping ? 'on' : '';
  $('chords').checked = o.chords;
  $('expression').checked = o.expression;
  $('undo').disabled = history.length === 0;
}

async function load() {
  const r = await fetch('/api/state');
  show(await r.json());
}

// 編集内容を変更してサーバーに送り、描き直す
async function commit(change, keepHistory = true) {
  const before = JSON.stringify(edits);
  const next = JSON.parse(before);
  change(next);
  if (JSON.stringify(next) === before) return;
  hidePop();
  const r = await fetch('/api/edits', {method: 'POST', body: JSON.stringify(next),
                                       headers: {'Content-Type': 'application/json'}});
  const data = await r.json();
  if (!r.ok) { $('msg').textContent = 'エラー: ' + data.error; show0(); return; }
  if (keepHistory) history.push(before);
  show(data);
}
function show0() { load(); }

function setOption(key, value) {
  commit(e => { e.options = e.options || {}; e.options[key] = value; });
}
for (const [name, label] of TUNINGS) $('tuning').add(new Option(label, name));
$('tuning').onchange = e => setOption('tuning', e.target.value);
$('capo').onchange = e => setOption('capo', Math.max(0, Math.min(12, parseInt(e.target.value) || 0)));
$('grid').onchange = e => setOption('grid', e.target.value ? parseInt(e.target.value) : null);
$('legato').onchange = e => commit(x => {
  x.options = x.options || {}; x.options.legato = e.target.checked;
  if (!e.target.checked) x.options.tapping = false; });
$('tapping').onchange = e => setOption('tapping', e.target.value === 'auto' ? 'auto' : e.target.value === 'on');
$('chords').onchange = e => setOption('chords', e.target.checked);
$('expression').onchange = e => setOption('expression', e.target.checked);
$('title').onchange = e => commit(x => { x.title = e.target.value.trim(); });
$('undo').onclick = async () => {
  if (!history.length) return;
  const prev = JSON.parse(history.pop());
  await commit(e => { for (const k of Object.keys(e)) delete e[k]; Object.assign(e, prev); }, false);
  $('undo').disabled = history.length === 0;
};
$('reset').onclick = () => {
  if (confirm('弦の変更・奏法記号・コード名・削除した音をすべて自動判定の状態に戻します。よろしいですか？'))
    commit(e => { delete e.notes; delete e.techs; delete e.chords; delete e.marks; });
};
$('pdf').onclick = async () => {
  $('pdf').disabled = true;
  try {
    const r = await fetch('/api/pdf');
    if (!r.ok) throw new Error((await r.json()).error);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await r.blob());
    a.download = ($('title').value || 'tab') + '.pdf';
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  } catch (err) {
    // サーバー側で作れないときはブラウザの印刷機能(PDFに保存)を使う
    $('msg').textContent = String(err.message || err);
    window.print();
  } finally { $('pdf').disabled = false; }
};

// ---- クリックで出る編集パネル
function hidePop() {
  $('pop').hidden = true;
  document.querySelectorAll('#score text.sel').forEach(t => t.classList.remove('sel'));
}
function el(tag, props, ...children) {
  const node = Object.assign(document.createElement(tag), props || {});
  node.append(...children);
  return node;
}
function stringLabel(s) { return (strings.length - s) + '弦'; }

function openPop(target, build) {
  hidePop();
  target.classList.add('sel');
  const pop = $('pop');
  pop.replaceChildren();
  build(pop);
  pop.hidden = false;
  const r = target.getBoundingClientRect();
  const left = Math.min(window.scrollX + r.left - 20, window.scrollX + document.documentElement.clientWidth - 365);
  pop.style.left = Math.max(8, left) + 'px';
  pop.style.top = (window.scrollY + r.bottom + 10) + 'px';
}

function chordEditor(pop, tick, current) {
  const input = el('input', {type: 'text', value: current, placeholder: '例: Am7'});
  const apply = () => commit(e => { e.chords = e.chords || {}; e.chords[tick] = input.value.trim(); });
  input.onkeydown = ev => { if (ev.key === 'Enter') apply(); };
  const row = el('div', {className: 'row'}, input, el('button', {textContent: '決定', onclick: apply}));
  if (current) row.append(el('button', {textContent: '消す',
    onclick: () => commit(e => { e.chords = e.chords || {}; e.chords[tick] = ''; })}));
  pop.append(el('h3', {textContent: 'この位置のコード名'}), row);
  return input;
}

function noteEditor(t) {
  const d = t.dataset, key = d.tick + ':' + d.pitch;
  openPop(t, pop => {
    pop.append(el('h3', {textContent: '弦を変える (今: ' + stringLabel(+d.string) + ' ' + d.fret + 'フレット)'}));
    const alts = d.alts ? d.alts.split(',') : [];
    const row = el('div', {className: 'row'});
    for (const a of alts) {
      const [s, f] = a.split(':').map(Number);
      row.append(el('button', {textContent: stringLabel(s) + ' ' + f + 'f',
        onclick: () => commit(e => { e.notes = e.notes || {}; e.notes[key] = {string: s}; })}));
    }
    if (!alts.length) row.append(el('span', {textContent: 'ほかに弾ける弦がありません'}));
    pop.append(row);

    pop.append(el('h3', {textContent: '前の音からのつなぎ'}));
    const techs = el('div', {className: 'row'});
    for (const [label, text] of [['', 'なし'], ...CONNECTIONS]) {
      techs.append(el('button', {textContent: text, className: d.tech === label ? 'on' : '',
        onclick: () => commit(e => { e.techs = e.techs || {}; e.techs[d.tick] = label; })}));
    }
    pop.append(techs);

    // この音に付ける記号 (複数可)
    const marks = d.marks ? d.marks.split(',') : [];
    const setMarks = list => commit(e => { e.marks = e.marks || {}; e.marks[d.tick] = list; });
    const bendSel = el('select', {onchange: ev => setMarks(
      marks.filter(c => !isBend(c)).concat(ev.target.value ? [ev.target.value] : []))});
    bendSel.add(new Option('なし', ''));
    for (const [code, text] of BENDS) bendSel.add(new Option(text, code));
    bendSel.value = marks.find(isBend) || '';
    pop.append(el('h3', {textContent: 'チョーキング'}), el('div', {className: 'row'}, bendSel));
    pop.append(el('h3', {textContent: '記号 (複数選べます)'}));
    const markRow = el('div', {className: 'row'});
    for (const [code, text] of MARKS) {
      const on = marks.includes(code);
      markRow.append(el('button', {textContent: text, className: on ? 'on' : '', onclick: () => {
        if (on) return setMarks(marks.filter(c => c !== code));
        // 同時に付けられない記号は入れ替える
        const clash = new Set(EXCLUSIVE.filter(g => g.includes(code)).flat());
        setMarks(marks.filter(c => !clash.has(c)).concat([code]));
      }}));
    }
    pop.append(markRow);
    chordEditor(pop, d.tick, d.chord);
    pop.append(el('h3', {textContent: 'その他'}),
      el('div', {className: 'row'}, el('button', {textContent: 'この音を譜面から消す', className: 'danger',
        onclick: () => commit(e => { e.notes = e.notes || {}; e.notes[key] = {hide: true}; })})));
  });
}

$('score').addEventListener('click', ev => {
  const t = ev.target.closest('text');
  if (!t) return;
  if (t.classList.contains('n')) noteEditor(t);
  else if (t.classList.contains('c'))
    openPop(t, pop => chordEditor(pop, t.dataset.tick, t.textContent).focus());
  ev.stopPropagation();
});
document.addEventListener('click', ev => { if (!$('pop').contains(ev.target)) hidePop(); });
document.addEventListener('keydown', ev => {
  if (ev.key === 'Escape') hidePop();
  if ((ev.ctrlKey || ev.metaKey) && ev.key === 'z' && ev.target.tagName !== 'INPUT') {
    ev.preventDefault(); $('undo').click();
  }
});
load();
</script>
</body>
</html>
"""


def _js(value) -> str:
    return json.dumps(value, ensure_ascii=False)


PAGE = (PAGE.replace("__PAGE_CSS__", tabsvg.PAGE_CSS)
        .replace("__TUNINGS__", _js([[name, f"{name} ({spec})"]
                                     for name, spec in midi2tab.TUNINGS.items()]))
        .replace("__CONNECTIONS__", _js(list(marklib.CONNECTIONS.items())))
        .replace("__MARKS__", _js(marklib.MARKS))
        .replace("__BENDS__", _js(marklib.BENDS))
        .replace("__EXCLUSIVE__", _js([sorted(g) for g in marklib.EXCLUSIVE])))
