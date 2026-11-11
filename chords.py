"""コードネームの自動判定。

小節 → 半分 → 1拍 と区間を区切り、その区間で鳴っている音の構成音(ピッチクラス)が
コードの型と一致するかを調べる。最低音がルートと違えば分数コードにする。
"""
from __future__ import annotations

# (名前の接尾辞, ルートからの音程)。上にあるものほど優先
TEMPLATES = [
    ("", (0, 4, 7)),
    ("m", (0, 3, 7)),
    ("7", (0, 4, 7, 10)),
    ("m7", (0, 3, 7, 10)),
    ("maj7", (0, 4, 7, 11)),
    ("sus4", (0, 5, 7)),
    ("sus2", (0, 2, 7)),
    ("dim", (0, 3, 6)),
    ("aug", (0, 4, 8)),
    ("6", (0, 4, 7, 9)),
    ("m6", (0, 3, 7, 9)),
    ("add9", (0, 2, 4, 7)),
    ("m(add9)", (0, 2, 3, 7)),
    ("m7b5", (0, 3, 6, 10)),
    ("dim7", (0, 3, 6, 9)),
    ("mMaj7", (0, 3, 7, 11)),
    ("7sus4", (0, 5, 7, 10)),
    ("9", (0, 2, 4, 7, 10)),
    ("maj9", (0, 2, 4, 7, 11)),
    ("m9", (0, 2, 3, 7, 10)),
]
# 5度を省略した形でも認める型
OMIT5 = {"7", "m7", "maj7", "9", "maj9", "m9"}

ROOT_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


def name_chord(pcs: set[int], bass: int, allow_power: bool = False,
               melodic: bool = False) -> str | None:
    """ピッチクラス集合と最低音(ピッチクラス)からコードネームを決める。

    melodic=True は音が重ならない単旋律の区間。スケールの断片をコードと
    誤認しないよう、2度を含む型と5度省略形は使わない。
    """
    best = None
    for prio, (suffix, intervals) in enumerate(TEMPLATES):
        if melodic and 2 in intervals:
            continue
        shapes = [set(intervals)]
        if suffix in OMIT5 and not melodic:
            shapes.append(set(intervals) - {7})
        for omitted, shape in enumerate(shapes):
            for root in range(12):
                if {(root + i) % 12 for i in shape} != pcs:
                    continue
                # ルートが最低音のものを優先、次に単純な型
                key = (root != bass, omitted, prio)
                if best is None or key < best[0]:
                    best = (key, root, suffix)
    if best is None:
        if allow_power and len(pcs) == 2 and (bass + 7) % 12 in pcs:
            return ROOT_NAMES[bass] + "5"
        return None
    _, root, suffix = best
    name = ROOT_NAMES[root] + suffix
    if bass != root:
        flat_side = "b" in ROOT_NAMES[root] or root == 5
        name += "/" + (FLAT_NAMES if flat_side else SHARP_NAMES)[bass]
    return name


def detect_chords(events, measures, grid: int, tpb: int) -> dict[int, str]:
    """ステップ → コードネーム(コードが変わる位置だけ)。

    events: ステップ → Event (step, ends={音高: 消音tick})
    measures: (開始ステップ, 長さ, 分子, 分母) のリスト
    """
    step_ticks = tpb * 4 / grid
    notes = [(ev.step * step_ticks, off, pitch, len(ev.ends))
             for ev in events.values() for pitch, off in ev.ends.items()]

    def window(a: int, b: int) -> tuple[str | None, set[int]]:
        """区間 [a, b) のコードネーム(なければ None)と、主な構成音。"""
        t0, t1 = a * step_ticks, b * step_ticks
        weight: dict[int, float] = {}   # ピッチクラス → 鳴っている長さ
        lowest: dict[int, int] = {}     # ピッチクラス → 最低の音高
        simultaneous = False
        spans = []
        for on, off, pitch, size in notes:
            overlap = min(off, t1) - max(on, t0)
            if overlap <= 0:
                continue
            spans.append((max(on, t0), min(off, t1)))
            pc = pitch % 12
            weight[pc] = weight.get(pc, 0.0) + overlap
            lowest[pc] = min(lowest.get(pc, 999), pitch)
            simultaneous |= size >= 2 and on >= t0
        if not weight:
            return None, set()
        # 一瞬だけ鳴る音(前のコードの残響や経過音)は無視する
        limit = 0.12 * max(weight.values())
        pcs = {pc for pc, w in weight.items() if w >= limit}
        if len(pcs) < 2:
            return None, pcs
        bass = min(lowest[pc] for pc in pcs) % 12
        spans.sort()
        melodic = all(y[0] >= x[1] - step_ticks / 4 for x, y in zip(spans, spans[1:]))
        return name_chord(pcs, bass, allow_power=simultaneous, melodic=melodic), pcs

    def detect(a: int, b: int, beat: int) -> list[tuple[int, str]]:
        nbeats = (b - a) // beat
        parts: list[tuple[int, str]] = []
        both = False
        if nbeats >= 2:
            mid = a + (nbeats + 1) // 2 * beat
            left, right = detect(a, mid, beat), detect(mid, b, beat)
            parts, both = left + right, bool(left and right)
        if both:
            return parts
        whole, pcs = window(a, b)
        if not whole:
            return parts
        if parts:
            # 片側だけで同じコードが取れている場合: その手前の音もコードの構成音なら
            # (アルペジオの弾き始めなど)区間の頭から、そうでなければ取れた位置から
            lead_in = window(a, parts[0][0])[1] if parts[0][0] > a else set()
            if any(name != whole for _, name in parts) or not lead_in <= pcs:
                return parts
        return [(a, whole)]

    found: list[tuple[int, str]] = []
    for start, length, num, den in measures:
        beat = max(1, grid // 4)
        if den == 8 and num % 3 == 0 and (3 * grid) % 8 == 0:
            beat = 3 * grid // 8
        found.extend(detect(start, start + length, beat))

    out: dict[int, str] = {}
    last = None
    for step, name in found:
        if name != last:
            out[step] = name
        last = name
    return out
