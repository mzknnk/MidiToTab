"""奏法記号の定義と、MIDIのピッチベンドからの自動判定。

記号は2種類ある:
  つなぎ(techs): 直前の音からのつながり方。1つの音に1つ。
  マーク(marks): その音自体に付く記号。複数付けられる。
"""
from __future__ import annotations

import bisect
import re

# 直前の音とのつなぎ: コード → 編集画面での名前
CONNECTIONS = {
    "H": "H ハンマリング",
    "P": "P プリング",
    "T": "T タッピング",
    "sl": "スライド (レガート)",
    "S": "スライド (弾き直す)",
}

# マーク: (コード, 編集画面での名前)
MARKS = [
    ("vib", "ビブラート"),
    ("pm", "パームミュート P.M."),
    ("x", "ミュート音 X"),
    ("ghost", "ゴーストノート ( )"),
    ("stac", "スタッカート"),
    ("acc", "アクセント >"),
    ("nh", "ナチュラルハーモニクス"),
    ("ah", "人工ハーモニクス A.H."),
    ("ph", "ピッキングハーモニクス P.H."),
    ("trem", "トレモロピッキング"),
    ("tr", "トリル"),
    ("si_up", "スライドイン (下から)"),
    ("si_down", "スライドイン (上から)"),
    ("so_up", "スライドアウト (上へ)"),
    ("so_down", "スライドアウト (下へ)"),
    ("down", "ダウンピッキング"),
    ("up", "アップピッキング"),
    ("slap", "スラップ"),
    ("pop", "プル (ポップ)"),
]
MARK_CODES = {code for code, _ in MARKS}
EXCLUSIVE = [{"x", "ghost", "nh"}, {"nh", "ah", "ph"}, {"down", "up"}, {"slap", "pop"},
             {"si_up", "si_down"}, {"so_up", "so_down"}]

# チョーキング: b=上げる, br=上げて戻す, pb=プリベンド, pbr=プリベンドして戻す。数字は半音数
BENDS = [
    ("b0.5", "¼ (クォーター)"), ("b1", "½ (半音)"), ("b2", "full (1音)"),
    ("b3", "1½ (1音半)"), ("b4", "2 (2音)"),
    ("br1", "½ → 戻す"), ("br2", "full → 戻す"), ("br3", "1½ → 戻す"),
    ("pb1", "プリベンド ½"), ("pb2", "プリベンド full"),
    ("pbr1", "プリベンド ½ → 戻す"), ("pbr2", "プリベンド full → 戻す"),
]
BEND_RE = re.compile(r"^(pbr|pb|br|b)(\d+(?:\.\d+)?)$")
BEND_LABELS = {0.5: "¼", 1.0: "½", 2.0: "full", 3.0: "1½", 4.0: "2"}
BEND_STEPS = [0.5, 1.0, 2.0, 3.0, 4.0]

# 譜表の上に文字で出すマーク
TEXT_MARKS = [("nh", "N.H."), ("ah", "A.H."), ("ph", "P.H."), ("tr", "tr"),
              ("slap", "slap"), ("pop", "pop"), ("acc", ">")]


def parse_bend(code: str) -> tuple[str, float] | None:
    m = BEND_RE.match(code)
    if not m or float(m.group(2)) not in BEND_LABELS:
        return None
    return m.group(1), float(m.group(2))


def find_bend(marks) -> tuple[str, float] | None:
    return next((b for b in map(parse_bend, marks) if b), None)


def clean_marks(codes) -> list[str]:
    """知らないコードと重複を除く。チョーキングは1つだけ残す。"""
    out: list[str] = []
    has_bend = False
    for code in codes if isinstance(codes, list) else []:
        if not isinstance(code, str) or code in out:
            continue
        if code in MARK_CODES:
            out.append(code)
        elif parse_bend(code) and not has_bend:
            out.append(code)
            has_bend = True
    return out


def bend_code(kind: str, semis: float) -> str:
    amount = min(BEND_STEPS, key=lambda s: abs(s - semis))
    return f"{kind}{amount:g}"


def detect_expression(song, events) -> dict[int, list[str]]:
    """ピッチベンドとモジュレーションから、チョーキングとビブラートを判定する。

    song.wheel: チャンネル → [(tick, 半音単位のずれ)]、song.mod: チャンネル → [(tick, CC1の値)]
    戻り値: ステップ → マークのリスト
    """
    out: dict[int, list[str]] = {}
    for step, ev in events.items():
        marks: list[str] = []
        t0, t1 = ev.on_tick, ev.end_tick
        curve = song.wheel.get(ev.channel)
        if curve:
            ticks = [t for t, _ in curve]
            i = bisect.bisect_right(ticks, t0)
            vals = [curve[i - 1][1] if i else 0.0]
            vals += [v for _, v in curve[i:bisect.bisect_left(ticks, t1)]]
            start, hi, lo, end = vals[0], max(vals), min(vals), vals[-1]
            # 上下に何度も揺れていればビブラート
            turns, direction = 0, 0
            for a, b in zip(vals, vals[1:]):
                d = (b > a + 0.02) - (b < a - 0.02)
                if d and d != direction:
                    turns += direction != 0
                    direction = d
            wobble = turns >= 3
            if wobble:
                marks.append("vib")
            if hi <= 5 and not (wobble and hi - lo <= 1.2 and start < 0.4):
                peak = vals.index(hi)
                released = end <= hi / 2 and peak < len(vals) - 1
                if start >= 0.4:
                    marks.append(bend_code("pbr" if end <= start / 2 else "pb", start))
                elif hi - max(start, 0.0) >= 0.2:
                    marks.append(bend_code("br" if released else "b", hi))
        mod = song.mod.get(ev.channel)
        if mod and "vib" not in marks:
            ticks = [t for t, _ in mod]
            i = bisect.bisect_right(ticks, t0)
            level = [mod[i - 1][1] if i else 0] + [v for _, v in mod[i:bisect.bisect_left(ticks, t1)]]
            if max(level) >= 32:
                marks.append("vib")
        if marks:
            out[step] = marks
    return out
