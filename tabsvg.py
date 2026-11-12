"""リズム表記つきTAB譜のHTML(SVG)描画。

1段(システム)ごとに1つの <svg> を出力する。弦の線とフレット番号の下に
符尾・連桁・付点・3連符、上に小節番号・let ring・奏法記号(H/P/T)を描く。
"""
from __future__ import annotations

from html import escape

import marks as marklib
from midi2tab import Item, Score, difficulty_level, pitch_class_name

PAGE_W = 1100
LEFT = 40          # 弦名ラベルの分
RIGHT = 10
TOP = 132          # 譜表より上(小節番号・let ring・奏法記号)
BOTTOM = 52        # 譜表より下(リズム)
GAP = 13           # 弦の間隔
PAD_L = 14         # 小節線から最初の音まで
TS_W = 28          # 拍子記号の幅
INK = "#1a1a1a"
GREY = "#8a8a8a"
LINE = "#a8a8a8"
LEVEL_COLORS = {1: "#9a9a9a", 2: "#9a9a9a", 3: "#b8860b", 4: "#d2691e", 5: "#c62828"}


def item_width(it: Item, score: Score) -> float:
    if it.whole_rest:
        return 64.0
    quarters = it.length * 4 / score.grid
    w = max(20.0, 34.0 * quarters ** 0.6)
    if it.step is not None and any(f >= 10 for _, f in score.fingerings[it.step]):
        w = max(w, 26.0)
    return w


def beat_length(score: Score, num: int, den: int) -> int:
    """連桁をまとめる1拍の長さ(ステップ数)。"""
    g = score.grid
    if den == 8 and num % 3 == 0 and (3 * g) % 8 == 0:
        return 3 * g // 8  # 6/8 などは付点4分でまとめる
    return max(1, g // 4)


def split_systems(score: Score) -> list[list[int]]:
    """小節(rhythm の index)を段に割り付ける。"""
    avail = PAGE_W - LEFT - RIGHT
    systems: list[list[int]] = []
    cur: list[int] = []
    used = 0.0
    for i, items in enumerate(score.rhythm):
        w = PAD_L + sum(item_width(it, score) for it in items)
        if shows_time_sig(score, i) or not cur:
            w += TS_W if shows_time_sig(score, i) else 0
        if cur and used + w > avail:
            systems.append(cur)
            cur, used = [], 0.0
        cur.append(i)
        used += w
    if cur:
        systems.append(cur)
    return systems


def shows_time_sig(score: Score, i: int) -> bool:
    m = score.measures[score.first_measure + i]
    if i == 0:
        return True
    prev = score.measures[score.first_measure + i - 1]
    return m[2:] != prev[2:]


def rest_glyph(cx: float, top: float, n_strings: int, it: Item) -> str:
    """休符。譜表の中央付近に描く。"""
    mid = top + GAP * (n_strings - 1) / 2
    k = it.dur.k
    if it.whole_rest or k == 0:
        y = top + GAP * ((n_strings - 1) // 2)
        g = f'<rect x="{cx - 7:.1f}" y="{y:.1f}" width="14" height="5.5" fill="{INK}"/>'
    elif k == 1:
        y = top + GAP * ((n_strings - 1) // 2 + 1)
        g = f'<rect x="{cx - 7:.1f}" y="{y - 5.5:.1f}" width="14" height="5.5" fill="{INK}"/>'
    elif k == 2:
        g = (f'<path d="M{cx - 3:.1f} {mid - 11:.1f} l6 7 l-6 6 l6 7 q-7 -2 -4 5" '
             f'fill="none" stroke="{INK}" stroke-width="2.2" stroke-linejoin="round"/>')
    else:
        flags = k - 2
        h = 6.0
        y0 = mid - (flags * h) / 2 - 2
        parts = [f'<line x1="{cx + 4:.1f}" y1="{y0:.1f}" x2="{cx - 1:.1f}" '
                 f'y2="{y0 + flags * h + 9:.1f}" stroke="{INK}" stroke-width="1.6"/>']
        for j in range(flags):
            y = y0 + 2 + j * h
            parts.append(f'<circle cx="{cx - 3.5 - j * 0.6:.1f}" cy="{y:.1f}" r="2.2" fill="{INK}"/>')
            parts.append(f'<line x1="{cx - 3.5 - j * 0.6:.1f}" y1="{y + 1.5:.1f}" '
                         f'x2="{cx + 3.6 - j * 1.6:.1f}" y2="{y - 0.5:.1f}" '
                         f'stroke="{INK}" stroke-width="1.4"/>')
        g = "".join(parts)
    if it.dur.dotted:
        g += f'<circle cx="{cx + 9:.1f}" cy="{mid - 3:.1f}" r="1.7" fill="{INK}"/>'
    return g


def draw_system(score: Score, measure_ids: list[int], last: bool) -> str:
    n = len(score.opens)
    staff_h = GAP * (n - 1)
    top = TOP
    bottom = top + staff_h
    height = top + staff_h + BOTTOM
    avail = PAGE_W - LEFT - RIGHT

    widths = {i: [item_width(it, score) for it in score.rhythm[i]] for i in measure_ids}
    fixed = sum(PAD_L + (TS_W if shows_time_sig(score, i) else 0) for i in measure_ids)
    flex = sum(sum(w) for w in widths.values())
    scale = (avail - fixed) / flex if flex else 1.0
    if last:
        scale = min(scale, 1.15)  # 最終段は引き伸ばしすぎない
    right = LEFT + fixed + flex * scale

    def string_y(s: int) -> float:
        return top + GAP * (n - 1 - s)  # 高音弦が上

    o: list[str] = []      # 背景(線)
    fg: list[str] = []     # 前景(数字・記号)
    for s in range(n):
        y = string_y(s)
        o.append(f'<line x1="{LEFT}" y1="{y}" x2="{right:.1f}" y2="{y}" stroke="{LINE}"/>')
        fg.append(f'<text x="{LEFT - 14}" y="{y + 4}" font-size="11" fill="{GREY}" '
                  f'text-anchor="middle">{escape(pitch_class_name(score.opens[s]))}</text>')
    o.append(f'<line x1="{LEFT}" y1="{top}" x2="{LEFT}" y2="{bottom}" stroke="{LINE}"/>')

    ys, ye = bottom + 10, bottom + 34   # 符尾の上端・下端
    event_cx: dict[int, float] = {}     # この段での各イベントの先頭x
    prev_cx: dict[int, float] = {}      # タイ用: イベントの直前の要素x
    half_w: dict[int, float] = {}       # 各イベントの数字の半幅
    # 「let ring ----|」のように範囲で示す記号: 名前 → [開始x, 終了x]
    spans: dict[str, list] = {"let ring": [None, 0.0], "P.M.": [None, 0.0]}
    span_y = {"let ring": top - 56, "P.M.": top - 69}
    span_text_w = {"let ring": 44, "P.M.": 30}

    def close_span(name: str):
        start, end = spans[name]
        if start is None:
            return
        y = span_y[name]
        x1 = start + span_text_w[name]
        x2 = max(end, x1 + 6)
        fg.append(f'<text x="{start - 4:.1f}" y="{y + 3.5}" font-size="10.5" '
                  f'font-style="italic" fill="{GREY}">{name}</text>')
        fg.append(f'<path d="M{x1:.1f} {y} H{x2:.1f} M{x2:.1f} {y - 4} v8" fill="none" '
                  f'stroke="{GREY}" stroke-dasharray="4 3"/>')
        spans[name][0] = None

    def track_span(name: str, active: bool, cx: float, tail: float):
        st = spans[name]
        if active:
            if st[0] is None:
                st[0] = cx
            st[1] = cx + 10
        elif st[0] is not None:
            st[1] = max(st[1], cx + tail)
            close_span(name)

    x = float(LEFT)
    for mi in measure_ids:
        ms, ml, num, den = score.measures[score.first_measure + mi]
        items = score.rhythm[mi]
        number = str(score.first_measure + mi + 1)
        fg.append(f'<text x="{x + 3:.1f}" y="{top - 7}" font-size="9.5" fill="{GREY}">'
                  f'{number}</text>')
        points = score.difficulty.get(score.first_measure + mi)
        if points is not None:  # 小節の難易度
            level = difficulty_level(points)
            fg.append(f'<text x="{x + 8 + 5.6 * len(number):.1f}" y="{top - 7}" font-size="8.5" '
                      f'font-weight="700" fill="{LEVEL_COLORS[level]}">Lv{level}</text>')
        section = score.sections.get(score.first_measure + mi)
        if section:  # セクション名 (MIDIのマーカー)
            w = 7.4 * len(section) + 12
            fg.append(f'<rect x="{x + 1:.1f}" y="{top - 118}" width="{w:.1f}" height="17" rx="2" '
                      f'fill="#fff" stroke="{INK}"/>')
            fg.append(f'<text x="{x + 7:.1f}" y="{top - 105.5}" font-size="12" font-weight="700" '
                      f'fill="{INK}">{escape(section)}</text>')
        if shows_time_sig(score, mi):
            cy = top + staff_h / 2
            for text, dy in ((num, -3), (den, 19)):
                fg.append(f'<text x="{x + TS_W / 2 + 4:.1f}" y="{cy + dy:.1f}" font-size="23" '
                          f'font-weight="700" text-anchor="middle" fill="{INK}">{text}</text>')
            x += TS_W
        mx0 = x
        cursor = x + PAD_L
        placed: list[tuple[Item, float]] = []
        for it, w in zip(items, widths[mi]):
            cx = cursor + 8
            if it.whole_rest:
                cx = cursor + w * scale / 2 - PAD_L / 2
            placed.append((it, cx))
            cursor += w * scale
        mx1 = cursor
        o.append(f'<line x1="{mx1:.1f}" y1="{top}" x2="{mx1:.1f}" y2="{bottom}" stroke="{LINE}"/>')

        # --- コードネーム(その位置以降で最初の要素の上に置く)
        for step in range(ms, ms + ml):
            name = score.chords.get(step)
            if name:
                cx = next((c for it, c in placed if it.start >= step), placed[-1][1])
                fg.append(f'<text class="c" data-tick="{score.tick(step)}" x="{cx - 5:.1f}" '
                          f'y="{top - 86}" font-size="13.5" fill="{INK}">{escape(name)}</text>')
        # --- 音符・休符
        for idx, (it, cx) in enumerate(placed):
            next_x = placed[idx + 1][1] if idx + 1 < len(placed) else mx1
            if it.step is None:
                fg.append(rest_glyph(cx, top, n, it))
                for name in spans:
                    close_span(name)
                continue
            fing = score.fingerings[it.step]
            if it.tied:
                x0 = max(prev_cx.get(it.step, cx - 22), LEFT + 2.0)
                lead = it.start == ms  # 小節頭のタイは (5) のように数字を再掲する
                for s, fret in fing:
                    if lead:
                        label = f"({fret})"
                        bw = 6.2 * len(label) + 2
                        fg.append(f'<rect x="{cx - bw / 2:.1f}" y="{string_y(s) - 6.5}" '
                                  f'width="{bw:.1f}" height="13" fill="#fff"/>')
                        fg.append(f'<text x="{cx:.1f}" y="{string_y(s) + 4}" font-size="11.5" '
                                  f'text-anchor="middle" fill="{GREY}">{label}</text>')
                    y = string_y(s) + 7
                    fg.append(f'<path d="M{x0 + 5:.1f} {y} Q{(x0 + cx) / 2:.1f} {y + 8} '
                              f'{cx - (9 if lead else 2):.1f} {y}" fill="none" stroke="{GREY}" stroke-width="1.3"/>')
            else:
                event_cx[it.step] = cx
                marks = score.marks.get(it.step, ())
                hw = 0.0
                for s, fret in fing:
                    y = string_y(s)
                    label = ("X" if "x" in marks else f"<{fret}>" if "nh" in marks
                             else f"({fret})" if "ghost" in marks else str(fret))
                    bw = 7.2 * len(label) + 3
                    hw = max(hw, bw / 2)
                    fg.append(f'<rect x="{cx - bw / 2:.1f}" y="{y - 6.5}" width="{bw:.1f}" '
                              f'height="13" fill="#fff"/>')
                    # data-* は編集画面用: 位置・音高・移せる弦(弦:フレット)・現在の記号
                    pitch = score.opens[s] + fret
                    used = {o for o, _ in fing if o != s}
                    alts = ",".join(
                        f"{o}:{pitch - p}" for o, p in enumerate(score.opens)
                        if o != s and o not in used and 0 <= pitch - p <= score.max_fret)
                    fg.append(f'<text class="n" data-tick="{score.tick(it.step)}" data-pitch="{pitch}" '
                              f'data-string="{s}" data-fret="{fret}" data-alts="{alts}" '
                              f'data-tech="{score.techs.get(it.step, ("",))[0]}" '
                              f'data-marks="{",".join(marks)}" '
                              f'data-chord="{escape(score.chords.get(it.step, ""), quote=True)}" '
                              f'x="{cx:.1f}" y="{y + 4.3}" font-size="12.5" font-weight="600" '
                              f'text-anchor="middle" fill="{INK}">{escape(label)}</text>')
                    # スライドイン / スライドアウト
                    for code, x1, y1, x2, y2 in (
                            ("si_up", -bw / 2 - 9, 4, -bw / 2 - 1, -2),
                            ("si_down", -bw / 2 - 9, -4, -bw / 2 - 1, 2),
                            ("so_up", bw / 2 + 1, 2, bw / 2 + 9, -4),
                            ("so_down", bw / 2 + 1, -2, bw / 2 + 9, 4)):
                        if code in marks:
                            fg.append(f'<line x1="{cx + x1:.1f}" y1="{y + y1}" x2="{cx + x2:.1f}" '
                                      f'y2="{y + y2}" stroke="{INK}" stroke-width="1.4"/>')
                half_w[it.step] = hw
                y_top = min(string_y(s) for s, _ in fing)  # 一番上にある音の弦

                # --- 直前の音とのつなぎ (H / P / T / スライド)
                tech = score.techs.get(it.step)
                if tech:
                    label, src = tech
                    lx = cx
                    y = string_y(fing[0][0])
                    slide = label in ("sl", "S")
                    if src in event_cx:
                        x0 = event_cx[src]
                        if slide:
                            d = 3 if fing[0][1] > score.fingerings[src][0][1] else -3
                            fg.append(f'<line x1="{x0 + half_w.get(src, 5) + 1:.1f}" y1="{y + d}" '
                                      f'x2="{cx - hw - 1:.1f}" y2="{y - d}" stroke="{INK}" stroke-width="1.4"/>')
                        if label != "S":  # スラー
                            fg.append(f'<path d="M{x0 + 3:.1f} {y - 8} Q{(x0 + cx) / 2:.1f} {y - 16} '
                                      f'{cx - 3:.1f} {y - 8}" fill="none" stroke="{INK}" stroke-width="1.2"/>')
                        if label != "T":
                            lx = (x0 + cx) / 2
                    elif slide:  # 前の音が前の段にある
                        fg.append(f'<line x1="{cx - hw - 9:.1f}" y1="{y + 3}" x2="{cx - hw - 1:.1f}" '
                                  f'y2="{y - 3}" stroke="{INK}" stroke-width="1.4"/>')
                    text = {"sl": "sl.", "S": ""}.get(label, label)
                    if text:
                        fg.append(f'<text x="{lx:.1f}" y="{top - 29}" font-size="10.5" font-weight="700" '
                                  f'text-anchor="middle" fill="{INK}">{text}</text>')

                # --- チョーキング (矢印と量)
                wave_x = cx - 3
                bend = marklib.find_bend(marks)
                if bend:
                    kind, semis = bend
                    tip, yb = top - 13, y_top - 4
                    if kind.startswith("pb"):  # プリベンド: 真上への矢印
                        bx = cx
                        fg.append(f'<line x1="{bx:.1f}" y1="{y_top - 8}" x2="{bx:.1f}" y2="{tip + 3}" '
                                  f'stroke="{INK}" stroke-width="1.3"/>')
                    else:
                        bx = cx + hw + 9
                        fg.append(f'<path d="M{cx + hw:.1f} {yb} Q{bx:.1f} {yb} {bx:.1f} {tip + 3}" '
                                  f'fill="none" stroke="{INK}" stroke-width="1.3"/>')
                    fg.append(f'<path d="M{bx - 3:.1f} {tip + 5} L{bx:.1f} {tip} L{bx + 3:.1f} {tip + 5} Z" '
                              f'fill="{INK}"/>')
                    fg.append(f'<text x="{bx:.1f}" y="{tip - 3}" font-size="9.5" text-anchor="middle" '
                              f'fill="{INK}">{marklib.BEND_LABELS[semis]}</text>')
                    wave_x = bx + 7
                    if kind.endswith("r"):  # 戻す: 下向きの矢印
                        rx = bx + 13
                        fg.append(f'<path d="M{bx:.1f} {tip} Q{rx:.1f} {tip} {rx:.1f} {yb - 3}" '
                                  f'fill="none" stroke="{INK}" stroke-width="1.3"/>')
                        fg.append(f'<path d="M{rx - 3:.1f} {yb - 5} L{rx:.1f} {yb} L{rx + 3:.1f} {yb - 5} Z" '
                                  f'fill="{INK}"/>')
                        wave_x = rx + 6

                # --- ビブラート / トリルの波線
                if "vib" in marks or "tr" in marks:
                    x_end = max(wave_x + 16, min(next_x - 8, wave_x + 72))
                    waves = int((x_end - wave_x) // 8)
                    fg.append(f'<path d="M{wave_x:.1f} {top - 8}{" q2 -3.4 4 0 t4 0" * waves}" '
                              f'fill="none" stroke="{INK}" stroke-width="1.4"/>')

                # --- 文字のマークとピッキング方向
                tx, anchor = cx, "middle"
                if "down" in marks:
                    fg.append(f'<path d="M{cx - 4:.1f} {top - 40} v-7 h8 v7 M{cx - 4:.1f} {top - 46.2} h8" '
                              f'fill="none" stroke="{INK}" stroke-width="1.5"/>')
                    tx, anchor = cx + 8, "start"
                elif "up" in marks:
                    fg.append(f'<path d="M{cx - 4:.1f} {top - 48} l4 8 l4 -8" fill="none" '
                              f'stroke="{INK}" stroke-width="1.4"/>')
                    tx, anchor = cx + 8, "start"
                texts = [text for code, text in marklib.TEXT_MARKS if code in marks]
                if texts:
                    fg.append(f'<text x="{tx:.1f}" y="{top - 41}" font-size="9.5" font-style="italic" '
                              f'text-anchor="{anchor}" fill="{INK}">{escape(" ".join(texts))}</text>')
                if "stac" in marks:
                    fg.append(f'<circle cx="{cx:.1f}" cy="{bottom + 5.5}" r="1.7" fill="{INK}"/>')

                track_span("let ring", it.step in score.ringing, cx, 4)
                track_span("P.M.", "pm" in marks, cx, 0)
            prev_cx[it.step] = cx
            # 符尾
            if not it.tied and "trem" in score.marks.get(it.step, ()):
                for j in range(3):  # トレモロピッキング
                    y = ys + 8 + j * 4.5
                    fg.append(f'<line x1="{cx - 4:.1f}" y1="{y + 1.5}" x2="{cx + 4:.1f}" '
                              f'y2="{y - 1.5}" stroke="{INK}" stroke-width="1.7"/>')
            k = it.dur.k
            if k >= 1:
                y0 = ys + 12 if k == 1 else ys
                o.append(f'<line x1="{cx:.1f}" y1="{y0}" x2="{cx:.1f}" y2="{ye}" '
                         f'stroke="{GREY}" stroke-width="1.4"/>')
            if it.dur.dotted:
                fg.append(f'<circle cx="{cx + 5:.1f}" cy="{ys + 3}" r="1.7" fill="{GREY}"/>')

        # --- 連桁・旗
        beat = beat_length(score, num, den)
        groups: list[list[tuple[Item, float]]] = []
        for it, cx in placed:
            beamable = it.step is not None and it.dur.k >= 3
            key = (it.start - ms) // beat
            if beamable and groups and groups[-1] and groups[-1][-1][0] is not None \
                    and (groups[-1][-1][0].start - ms) // beat == key \
                    and groups[-1][-1][0].start + groups[-1][-1][0].length == it.start:
                groups[-1].append((it, cx))
            elif beamable:
                groups.append([(it, cx)])
            else:
                groups.append([])  # 区切り
        for grp in groups:
            if not grp:
                continue
            nb = [it.dur.k - 2 for it, _ in grp]
            if len(grp) == 1:
                cx = grp[0][1]
                for lv in range(nb[0]):
                    y = ye - lv * 5
                    o.append(f'<path d="M{cx:.1f} {y} q5 -2 6 -9" fill="none" '
                             f'stroke="{GREY}" stroke-width="1.8"/>')
                continue
            for lv in range(1, max(nb) + 1):
                y = ye - (lv - 1) * 5
                for j, (_, cx) in enumerate(grp):
                    if nb[j] < lv:
                        continue
                    if j + 1 < len(grp) and nb[j + 1] >= lv:
                        x1, x2 = cx, grp[j + 1][1]
                    elif j > 0 and nb[j - 1] >= lv:
                        continue
                    elif j > 0:
                        x1, x2 = cx - 7, cx
                    else:
                        x1, x2 = cx, cx + 7
                    o.append(f'<rect x="{x1 - 0.7:.1f}" y="{y - 3}" width="{x2 - x1 + 1.4:.1f}" '
                             f'height="3" fill="{GREY}"/>')
        # --- 3連符の「3」
        trip: dict[int, list[float]] = {}
        for it, cx in placed:
            if it.dur.triplet:
                trip.setdefault((it.start - ms) // beat, []).append(cx)
        for xs in trip.values():
            fg.append(f'<text x="{sum(xs) / len(xs):.1f}" y="{ye + 12}" font-size="10" '
                      f'font-style="italic" text-anchor="middle" fill="{GREY}">3</text>')
        x = mx1
        del mx0
    for name, st in spans.items():
        if st[0] is not None and name == "let ring":
            st[1] = max(st[1], x - 6)
        close_span(name)

    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PAGE_W} {height}" '
            f'font-family="Helvetica, Arial, sans-serif">{"".join(o)}{"".join(fg)}</svg>')


PAGE_CSS = f"""
  body {{ background: #fff; color: #1a1a1a; margin: 24px auto; padding: 0 16px;
         max-width: {PAGE_W}px; font-family: Helvetica, Arial, "Hiragino Sans", "Noto Sans CJK JP", sans-serif; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .meta {{ color: #666; font-size: 14px; margin: 0 0 12px; }}
  svg {{ display: block; width: 100%; height: auto; break-inside: avoid; }}
  @page {{ size: A4; margin: 12mm; }}
  @media print {{ body {{ margin: 0; padding: 0; max-width: none; }} .no-print {{ display: none !important; }} }}
"""


def render_systems(score: Score) -> list[str]:
    """段ごとのSVG文字列。"""
    systems = split_systems(score)
    return [draw_system(score, ids, i == len(systems) - 1) for i, ids in enumerate(systems)]


def meta_line(score: Score) -> str:
    meta = []
    if score.bpm:
        meta.append(f"♩ = {score.bpm:.0f}")
    meta.append("Tuning: " + " ".join(pitch_class_name(p) for p in score.opens))
    if score.capo:
        meta.append(f"Capo {score.capo}")
    if score.difficulty:
        levels = [difficulty_level(p) for p in score.difficulty.values()]
        meta.append(f"難易度: 最大 Lv{max(levels)} / 平均 Lv{sum(levels) / len(levels):.1f}")
    return "  ·  ".join(meta)


def render_html(score: Score, title: str) -> str:
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<h1>{escape(title)}</h1>
<p class="meta">{escape(meta_line(score))}</p>
{chr(10).join(render_systems(score))}
</body>
</html>
"""
