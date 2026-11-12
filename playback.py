"""再生用のデータ作り。譜面(Score)から「いつ・どの音を・どれだけ」鳴らすかの一覧を作る。

音そのものは編集画面(ブラウザ)側で合成する。ここでは時刻の計算だけを行う。
"""
from __future__ import annotations

import bisect

from midi2tab import Score

MAX_NOTE_SECONDS = 6.0


def tick_to_seconds(tempos: list[tuple[int, int]], tpb: int):
    """テンポ変更を反映して tick を秒に直す関数を返す。tempos: [(tick, 1拍のマイクロ秒)]"""
    points = sorted(tempos)
    if not points or points[0][0] > 0:
        points.insert(0, (0, points[0][1] if points else 500000))
    table = []  # (tick, その時点までの秒, 1拍のマイクロ秒)
    seconds = 0.0
    for i, (tick, usec) in enumerate(points):
        if i:
            prev_tick, _, prev_usec = table[-1]
            seconds += (tick - prev_tick) * prev_usec / 1e6 / tpb
        table.append((tick, seconds, usec))
    ticks = [t for t, _, _ in table]

    def convert(tick: float) -> float:
        t0, s0, usec = table[max(0, bisect.bisect_right(ticks, tick) - 1)]
        return s0 + (tick - t0) * usec / 1e6 / tpb

    return convert


def playback_notes(score: Score) -> dict:
    """再生する音の一覧。時刻は譜面の最初の小節の頭を 0 秒とする。

    各音: t=開始秒, d=長さ秒, p=音高, s=弦, k=譜面上の位置(tick), m=マーク,
          c=前の音からのつなぎ, q=スライド元の音高
    """
    seconds = tick_to_seconds(score.tempos, score.tpb)
    start = seconds(score.tick(score.measures[score.first_measure][0]))
    notes = []
    for step, fing in score.fingerings.items():
        tick = score.tick(step)
        t = seconds(tick) - start
        marks = score.marks.get(step, [])
        tech = score.techs.get(step)
        ends = score.events[step].ends
        for string, fret in fing:
            pitch = score.opens[string] + fret
            if "x" in marks:
                dur = 0.06
            else:
                end = ends.get(pitch, tick + score.tpb)
                dur = min(MAX_NOTE_SECONDS, max(0.08, seconds(end) - seconds(tick)))
            note = {"t": round(t, 4), "d": round(dur, 4), "p": pitch, "s": string, "k": tick}
            if marks:
                note["m"] = marks
            if tech:
                note["c"] = tech[0]
                src = tech[1]
                if tech[0] in ("sl", "S") and src in score.fingerings:
                    s0, f0 = score.fingerings[src][0]
                    note["q"] = score.opens[s0] + f0
            notes.append(note)
    notes.sort(key=lambda n: (n["t"], n["s"]))
    last = score.measures[-1]
    length = seconds(score.tick(last[0] + last[1])) - start
    return {"notes": notes, "length": round(length, 3)}
