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
import playback
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
                "playback": playback.playback_notes(score),
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
  #score { position: relative; }
  #score text.playing { fill: #d0452f; }
  #cursor { position: absolute; width: 2px; background: rgba(208, 69, 47, .55); pointer-events: none; }
  #clock { font-variant-numeric: tabular-nums; color: #555; min-width: 78px; }
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
  <label><input id="textures" type="checkbox"> 奏法の種類を表示</label>
  <span class="spacer"></span>
  <button id="play" class="primary">▶ 再生</button>
  <label>速さ
    <select id="speed">
      <option value="0.5">50%</option><option value="0.75">75%</option>
      <option value="1" selected>100%</option><option value="1.25">125%</option>
    </select></label>
  <span id="clock">0:00 / 0:00</span>
  <button id="undo">元に戻す</button>
  <button id="reset">手直しをリセット</button>
  <button id="pdf" class="primary">PDFで保存</button>
</div>
<input id="title" type="text" aria-label="タイトル">
<p class="meta" id="meta"></p>
<p id="msg" class="no-print"></p>
<div id="score"></div>
<p class="hint no-print">再生: 「▶ 再生」またはスペースキー。数字をクリックして「ここから再生」で途中から。数字をクリック: 弦の変更・奏法記号(スライド、チョーキング、ビブラートなど)・コード名・音の削除。コード名をクリック: 名前の変更。変更は自動で保存されます。</p>
<div id="pop" class="no-print" hidden></div>
<script>
const TUNINGS = __TUNINGS__;
const CONNECTIONS = __CONNECTIONS__, MARKS = __MARKS__, BENDS = __BENDS__, EXCLUSIVE = __EXCLUSIVE__;
const isBend = c => /^(pbr|pb|br|b)[0-9.]+$/.test(c);
const $ = id => document.getElementById(id);
let edits = {}, history = [], strings = [];
let song = {notes: [], length: 0};   // 再生用のデータ

function show(state) {
  stopPlayback();
  edits = state.edits; strings = state.strings;
  song = state.playback;
  showClock(0);
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
  $('textures').checked = o.textures;
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
$('textures').onchange = e => setOption('textures', e.target.checked);
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
      el('div', {className: 'row'}, el('button', {textContent: '▶ ここから再生', onclick: () => {
        const first = song.notes.find(n => String(n.k) === d.tick);
        hidePop();
        if (first) startPlayback(first.t);
      }}), el('button', {textContent: 'この音を譜面から消す', className: 'danger',
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
  if (ev.key === ' ' && !['INPUT', 'SELECT', 'BUTTON', 'TEXTAREA'].includes(ev.target.tagName)) {
    ev.preventDefault(); $('play').click();
  }
  if ((ev.ctrlKey || ev.metaKey) && ev.key === 'z' && ev.target.tagName !== 'INPUT') {
    ev.preventDefault(); $('undo').click();
  }
});

// ---- 再生: 弦をはじく音をブラウザ内で合成して鳴らす
let audio = null, master = null, player = null;
const plucks = new Map();   // 音高と種類ごとの合成済みの音

// Karplus-Strong 法: ノイズを短い遅延線で回し、少しずつ丸めていくと弦の音になる
function pluckBuffer(pitch, kind) {
  const key = pitch + kind;
  if (plucks.has(key)) return plucks.get(key);
  const sr = audio.sampleRate, freq = 440 * Math.pow(2, (pitch - 69) / 12);
  const seconds = kind === 'dead' ? 0.12 : kind === 'mute' ? 0.5 : 4.5;
  const t60 = kind === 'dead' ? 0.035 : kind === 'mute' ? 0.22 : Math.max(1.6, 5.5 - (pitch - 40) * 0.07);
  const smooth = kind === 'normal' ? 0.5 : 0.62;            // 大きいほど高い倍音が早く消える
  const period = sr / freq - smooth;                        // 平均化で生じる遅れの分を引く
  const N = Math.floor(period), frac = period - N;
  const decay = Math.pow(10, -3 / (t60 * freq));
  const n = Math.floor(sr * seconds), out = new Float32Array(n);
  let prevNoise = 0;
  for (let i = 0; i < N + 2 && i < n; i++) {                // はじいた瞬間のノイズ(少し丸める)
    const r = Math.random() * 2 - 1;
    out[i] = 0.55 * r + 0.45 * prevNoise;
    prevNoise = r;
  }
  for (let i = N + 2; i < n; i++) {
    const a = out[i - N] * (1 - frac) + out[i - N - 1] * frac;
    const b = out[i - N - 1] * (1 - frac) + out[i - N - 2] * frac;
    out[i] = decay * ((1 - smooth) * a + smooth * b);
  }
  let peak = 0;
  for (let i = 0; i < n; i++) peak = Math.max(peak, Math.abs(out[i]));
  const fade = Math.min(n, Math.floor(sr * 0.002));
  for (let i = 0; i < n; i++) out[i] *= 0.5 / (peak || 1) * (i < fade ? i / fade : 1);
  const buffer = audio.createBuffer(1, n, sr);
  buffer.copyToChannel(out, 0);
  plucks.set(key, buffer);
  return buffer;
}

const isBendCode = c => /^(pbr|pb|br|b)[0-9.]+$/.test(c);
const ratio = semis => Math.pow(2, semis / 12);

// 1音を鳴らす。when は鳴らし始める時刻、dur は長さ(どちらもオーディオ側の秒)
function playNote(note, when, dur) {
  const marks = note.m || [];
  const kind = marks.includes('x') ? 'dead' : marks.includes('pm') ? 'mute' : 'normal';
  const src = audio.createBufferSource();
  src.buffer = pluckBuffer(note.p, kind);
  const gain = audio.createGain();
  let level = kind === 'dead' ? 0.55 : 0.8;
  if (['H', 'P', 'T', 'sl'].includes(note.c)) level *= 0.7;   // 弾き直さない音は弱め
  if (marks.includes('ghost')) level *= 0.45;
  if (marks.includes('acc')) level *= 1.25;
  const end = when + dur;
  gain.gain.setValueAtTime(level, when);
  gain.gain.setValueAtTime(level, end);
  gain.gain.exponentialRampToValueAtTime(0.001, end + 0.09);
  const rate = src.playbackRate;
  rate.setValueAtTime(1, when);
  if (note.q != null && note.q !== note.p) {                  // 前の音からスライド
    rate.setValueAtTime(ratio(note.q - note.p), when);
    rate.linearRampToValueAtTime(1, when + Math.min(0.08, dur / 2));
  } else if (marks.includes('si_up') || marks.includes('si_down')) {
    rate.setValueAtTime(ratio(marks.includes('si_up') ? -2 : 2), when);
    rate.linearRampToValueAtTime(1, when + Math.min(0.07, dur / 2));
  }
  const bend = marks.find(isBendCode);
  if (bend) {                                                 // チョーキング
    const m = bend.match(/^(pbr|pb|br|b)([0-9.]+)$/), up = ratio(parseFloat(m[2]));
    if (m[1] === 'b') { rate.linearRampToValueAtTime(up, when + Math.min(0.3, dur * 0.5)); }
    if (m[1] === 'br') {
      rate.linearRampToValueAtTime(up, when + dur * 0.35);
      rate.setValueAtTime(up, when + dur * 0.55);
      rate.linearRampToValueAtTime(1, when + dur * 0.85);
    }
    if (m[1] === 'pb') rate.setValueAtTime(up, when);
    if (m[1] === 'pbr') { rate.setValueAtTime(up, when); rate.linearRampToValueAtTime(1, when + dur * 0.6); }
  }
  if (marks.includes('so_down') || marks.includes('so_up')) { // 音の終わりで滑らせる
    const slide = Math.min(0.14, dur / 2);
    rate.setValueAtTime(1, end - slide);
    rate.linearRampToValueAtTime(ratio(marks.includes('so_down') ? -5 : 5), end + 0.06);
  }
  if ((marks.includes('vib') || marks.includes('tr')) && dur > 0.2) {   // ビブラート
    const lfo = audio.createOscillator(), depth = audio.createGain();
    lfo.frequency.value = 5.5;
    depth.gain.value = marks.includes('tr') ? 100 : 28;       // セント
    lfo.connect(depth).connect(src.detune);
    lfo.start(when + 0.1); lfo.stop(end + 0.1);
  }
  src.connect(gain).connect(master);
  src.start(when); src.stop(end + 0.12);
  return src;
}

function showClock(t) {
  const f = s => Math.floor(s / 60) + ':' + String(Math.floor(s % 60)).padStart(2, '0');
  $('clock').textContent = f(Math.max(0, t)) + ' / ' + f(song.length || 0);
}

function startPlayback(from = 0) {
  stopPlayback();
  if (!song.notes.length) return;
  if (!audio) {
    audio = new (window.AudioContext || window.webkitAudioContext)();
    const tone = audio.createBiquadFilter();                  // 胴鳴りの代わりに高域を少し落とす
    tone.type = 'lowpass'; tone.frequency.value = 5200;
    const limiter = audio.createDynamicsCompressor();
    master = audio.createGain(); master.gain.value = 0.55;
    master.connect(tone).connect(limiter).connect(audio.destination);
  }
  audio.resume();
  const byTick = new Map();                                   // 譜面上の位置 → その数字の要素
  document.querySelectorAll('#score text.n').forEach(t => {
    if (!byTick.has(t.dataset.tick)) byTick.set(t.dataset.tick, []);
    byTick.get(t.dataset.tick).push(t);
  });
  const cursor = el('div', {id: 'cursor'});
  $('score').append(cursor);
  player = {
    speed: parseFloat($('speed').value), pos0: from, time0: audio.currentTime + 0.12,
    next: song.notes.findIndex(n => n.t >= from - 1e-6), live: [], scheduled: 0,
    byTick, cursor, shown: null, seen: -1,
  };
  if (player.next < 0) player.next = song.notes.length;
  $('play').textContent = '■ 停止';
  player.timer = setInterval(schedule, 40);
  schedule();
  player.frame = requestAnimationFrame(follow);
}

function songTime() { return player.pos0 + (audio.currentTime - player.time0) * player.speed; }

// 少し先までの音を予約する(まとめて予約すると停止や速さの変更が効かないため)
function schedule() {
  if (!player) return;
  const horizon = songTime() + 0.35 * player.speed;
  while (player.next < song.notes.length && song.notes[player.next].t <= horizon) {
    const note = song.notes[player.next++];
    const strum = note.s * 0.006;                             // 和音は低音弦から少しずらす
    const when = Math.max(audio.currentTime, player.time0 + (note.t - player.pos0) / player.speed) + strum;
    player.live.push(playNote(note, when, note.d / player.speed));
    player.scheduled++;
  }
  if (player.live.length > 400) player.live.splice(0, 200);
  if (songTime() > song.length + 0.5) stopPlayback();
}

// 再生位置にあわせて、いま鳴っている数字を強調し、カーソルを動かす
function follow() {
  if (!player) return;
  const now = songTime();
  showClock(now);
  let i = player.seen;
  while (i + 1 < song.notes.length && song.notes[i + 1].t <= now) i++;
  if (i !== player.seen && i >= 0) {
    player.seen = i;
    const tick = String(song.notes[i].k);
    if (tick !== player.shown) {
      (player.byTick.get(player.shown) || []).forEach(t => t.classList.remove('playing'));
      player.shown = tick;
      const targets = player.byTick.get(tick) || [];
      targets.forEach(t => t.classList.add('playing'));
      if (targets.length) {
        const box = $('score').getBoundingClientRect();
        const staff = targets[0].closest('svg').getBoundingClientRect();
        const r = targets[0].getBoundingClientRect();
        Object.assign(player.cursor.style, {
          left: (r.left + r.width / 2 - box.left - 1) + 'px',
          top: (staff.top - box.top + staff.height * 0.36) + 'px', height: (staff.height * 0.5) + 'px'});
        if (staff.top < 70 || staff.bottom > window.innerHeight - 10)
          window.scrollTo({top: window.scrollY + staff.top - 120, behavior: 'smooth'});
      }
    }
  }
  player.frame = requestAnimationFrame(follow);
}

function stopPlayback() {
  if (!player) return;
  clearInterval(player.timer);
  cancelAnimationFrame(player.frame);
  player.live.forEach(src => { try { src.stop(); } catch (err) {} });
  document.querySelectorAll('#score text.playing').forEach(t => t.classList.remove('playing'));
  player.cursor.remove();
  window.lastPlayback = {scheduled: player.scheduled, position: songTime()};  // 動作確認用
  player = null;
  $('play').textContent = '▶ 再生';
}

$('play').onclick = () => player ? stopPlayback() : startPlayback(0);
$('speed').onchange = () => {                                 // 再生中に速さを変えたら、その位置から続ける
  if (!player) return;
  const now = songTime();
  player.pos0 = now; player.time0 = audio.currentTime; player.speed = parseFloat($('speed').value);
};
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
