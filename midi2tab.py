#!/usr/bin/env python3
"""midi2tab - ギターのMIDIファイルをTAB譜に書き起こす。

処理の流れ:
  1. MIDIからノート(発音位置・長さ・音高)を取り出す
  2. 発音位置をグリッド(16分音符など)にクオンタイズし、同時発音を和音にまとめる
  3. 各和音について「どの弦の何フレットで弾くか」の候補を列挙し、
     左手のポジション移動が最小になる並びを動的計画法(ビタビ)で選ぶ
  4. 音の長さを音符・休符・タイに分解する
  5. ASCII、またはリズム表記つきのHTML(SVG)として出力する
"""
from __future__ import annotations

import argparse
import bisect
import collections
import itertools
import json
import os
import sys
from dataclasses import dataclass, field

import mido

import chords as chordlib
import marks as marklib

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# 低音弦 → 高音弦 の順
TUNINGS = {
    "standard": "E2 A2 D3 G3 B3 E4",
    "dropd": "D2 A2 D3 G3 B3 E4",
    "halfdown": "D#2 G#2 C#3 F#3 A#3 D#4",
    "dadgad": "D2 A2 D3 G3 A3 D4",
    "openg": "D2 G2 D3 G3 B3 D4",
    "7string": "B1 E2 A2 D3 G3 B3 E4",
    "bass": "E1 A1 D2 G2",
    "ukulele": "G4 C4 E4 A4",
}

AUTO_GRIDS = [4, 8, 12, 16, 24, 32, 48]
MAX_CANDIDATES = 40  # 和音ごとに保持する運指候補の上限
HAND_REACH = 3       # 手を動かさずに届く範囲: ポジション p なら p〜p+3 フレット
MAX_GROUP_COST = 3.5  # アルペジオを1つのフォームとみなす上限(これより弾きにくければ分ける)
TAP_COST = 2.5       # 右手タッピング1音のコスト(ポジション移動との比較用)


# ---------------------------------------------------------------- 音名まわり

def parse_note(name: str) -> int:
    """'E2' や 'F#3', 'Bb1' をMIDIノート番号に変換する (C4 = 60)。"""
    s = name.strip()
    if len(s) < 2:
        raise ValueError(f"音名を解釈できません: {name!r}")
    letter = s[0].upper()
    rest = s[1:]
    base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}.get(letter)
    if base is None:
        raise ValueError(f"音名を解釈できません: {name!r}")
    while rest and rest[0] in "#b":
        base += 1 if rest[0] == "#" else -1
        rest = rest[1:]
    try:
        octave = int(rest)
    except ValueError:
        raise ValueError(f"音名を解釈できません: {name!r}") from None
    return base + 12 * (octave + 1)


def parse_tuning(spec: str) -> list[int]:
    """プリセット名、または 'E2 A2 D3 G3 B3 E4' 形式(低音弦から)を解釈する。"""
    spec = TUNINGS.get(spec.lower(), spec)
    return [parse_note(tok) for tok in spec.replace(",", " ").split()]


def pitch_class_name(pitch: int) -> str:
    return NOTE_NAMES[pitch % 12]


# ---------------------------------------------------------------- MIDI読み込み

@dataclass
class Note:
    on: int   # tick
    off: int  # tick
    pitch: int
    channel: int = 0


@dataclass
class Song:
    ticks_per_beat: int
    notes: list[Note]
    time_sigs: list[tuple[int, int, int]]  # (tick, 分子, 分母)
    bpm: float | None
    wheel: dict[int, list[tuple[int, float]]] = field(default_factory=dict)  # ch → (tick, 半音)
    mod: dict[int, list[tuple[int, int]]] = field(default_factory=dict)      # ch → (tick, CC1)
    dead: list[int] = field(default_factory=list)                 # 空ピッキングのtick
    markers: list[tuple[int, str]] = field(default_factory=list)  # (tick, セクション名)


@dataclass
class TrackInfo:
    index: int
    name: str
    notes: int
    channels: set[int] = field(default_factory=set)
    low: int | None = None
    high: int | None = None
    instrument: str = ""
    first_tick: int | None = None
    last_tick: int = 0


def scan_tracks(mid: mido.MidiFile) -> list[TrackInfo]:
    infos = []
    for i, track in enumerate(mid.tracks):
        info = TrackInfo(i, "", 0)
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type == "track_name" and not info.name:
                info.name = msg.name.strip()
            elif msg.type == "instrument_name" and not info.instrument:
                info.instrument = msg.name.strip()
            elif msg.type == "note_on" and msg.velocity > 0:
                if info.first_tick is None:
                    info.first_tick = tick
                info.last_tick = tick
                info.notes += 1
                info.channels.add(msg.channel)
                info.low = msg.note if info.low is None else min(info.low, msg.note)
                info.high = msg.note if info.high is None else max(info.high, msg.note)
        infos.append(info)
    return infos


def load_song(mid: mido.MidiFile, tracks: list[int] | None = None,
              channels: list[int] | None = None, transpose: int = 0,
              dead_notes: frozenset[int] = frozenset()) -> Song:
    """ノート(発音tick, 消音tick, 音高)を集める。ドラム(ch10)は既定で除外。"""
    notes: list[Note] = []
    time_sigs = []
    bpm = None
    wheel: dict[int, list[tuple[int, float]]] = {}
    mod: dict[int, list[tuple[int, int]]] = {}
    bend_range: dict[int, float] = {}  # ch → ピッチベンドの幅(半音)。既定は±2
    rpn: dict[int, list[int]] = {}
    dead: list[int] = []
    markers: list[tuple[int, str]] = []
    for i, track in enumerate(mid.tracks):
        tick = 0
        sounding: dict[tuple[int, int], list[Note]] = {}
        for msg in track:
            tick += msg.time
            if msg.type == "time_signature":
                time_sigs.append((tick, msg.numerator, msg.denominator))
            elif msg.type == "set_tempo" and bpm is None:
                bpm = mido.tempo2bpm(msg.tempo)
            elif msg.type == "pitchwheel":
                semis = msg.pitch / 8192 * bend_range.get(msg.channel, 2.0)
                wheel.setdefault(msg.channel, []).append((tick, semis))
            elif msg.type == "control_change":
                if msg.control == 1:
                    mod.setdefault(msg.channel, []).append((tick, msg.value))
                elif msg.control in (100, 101):
                    rpn.setdefault(msg.channel, [127, 127])[msg.control - 100] = msg.value
                elif msg.control == 6 and rpn.get(msg.channel) == [0, 0]:
                    bend_range[msg.channel] = float(msg.value)
            elif msg.type == "marker":
                markers.append((tick, msg.text.strip()))
            elif msg.type in ("note_on", "note_off"):
                if tracks is not None and i not in tracks:
                    continue
                if channels is not None:
                    if msg.channel not in channels:
                        continue
                elif msg.channel == 9:
                    continue
                if msg.note in dead_notes:  # 空ピッキング用のキー
                    if msg.type == "note_on" and msg.velocity > 0:
                        dead.append(tick)
                    continue
                key = (msg.channel, msg.note)
                if msg.type == "note_on" and msg.velocity > 0:
                    note = Note(tick, -1, msg.note + transpose, msg.channel)
                    notes.append(note)
                    sounding.setdefault(key, []).append(note)
                elif sounding.get(key):
                    sounding[key].pop(0).off = tick
    for n in notes:
        if n.off <= n.on:  # ノートオフが無い/長さ0
            n.off = n.on + mid.ticks_per_beat
    notes.sort(key=lambda n: (n.on, n.pitch))
    time_sigs.sort()
    if not time_sigs or time_sigs[0][0] > 0:
        time_sigs.insert(0, (0, 4, 4))
    for curve in list(wheel.values()) + list(mod.values()):
        curve.sort(key=lambda p: p[0])
    dead = sorted(set(dead))
    for n in notes:  # 空ピッキングは弦を止めるので、鳴っている音はそこで切る
        j = bisect.bisect_right(dead, n.on)
        if j < len(dead) and dead[j] < n.off:
            n.off = dead[j]
    return Song(mid.ticks_per_beat, notes, time_sigs, bpm, wheel, mod, dead, sorted(markers))


# ---------------------------------------------------------------- クオンタイズ

@dataclass
class Event:
    """同じグリッド位置で鳴る音のまとまり(単音または和音)。"""
    step: int
    on_tick: int
    ends: dict[int, int] = field(default_factory=dict)  # 音高 → 消音tick
    channel: int = 0

    @property
    def pitches(self) -> list[int]:
        return sorted(self.ends)

    @property
    def end_tick(self) -> int:
        return max(self.ends.values())

    @property
    def min_end_tick(self) -> int:
        return min(self.ends.values())


def choose_grid(song: Song) -> int:
    """全ノートがほぼ乗る最も粗いグリッドを選ぶ。見つからなければ16分。"""
    for grid in AUTO_GRIDS:
        step = song.ticks_per_beat * 4 / grid
        tol = min(song.ticks_per_beat / 24, step / 4)
        if any((num * grid) % den for _, num, den in song.time_sigs):
            continue  # 小節がグリッドで割り切れない
        ok = sum(1 for n in song.notes if abs(n.on - round(n.on / step) * step) <= tol)
        if ok >= 0.97 * len(song.notes):
            return grid
    return 16


def quantize(song: Song, grid: int) -> dict[int, Event]:
    """ステップ番号 → Event。"""
    step = song.ticks_per_beat * 4 / grid
    events: dict[int, Event] = {}
    for n in song.notes:
        s = round(n.on / step)
        ev = events.setdefault(s, Event(s, n.on, channel=n.channel))
        ev.on_tick = min(ev.on_tick, n.on)
        ev.ends[n.pitch] = max(ev.ends.get(n.pitch, 0), n.off)
    return dict(sorted(events.items()))


Measure = tuple[int, int, int, int]  # (開始ステップ, 長さ, 拍子の分子, 分母)


def build_measures(song: Song, grid: int, last_step: int) -> list[Measure]:
    """last_step までを覆う小節リスト。拍子変更に対応。"""
    step = song.ticks_per_beat * 4 / grid
    sigs = [(round(t / step), n, d) for t, n, d in song.time_sigs]
    measures = []
    cur = 0
    while cur <= last_step:
        num, den = next((n, d) for s, n, d in reversed(sigs) if s <= cur)
        length = max(1, round(num * grid / den))
        measures.append((cur, length, num, den))
        cur += length
    return measures


# ---------------------------------------------------------------- 運指の決定

Fingering = tuple[tuple[int, int], ...]  # ((弦index, フレット), ...) 弦indexは低音弦=0


@dataclass
class Assignment:
    fingerings: dict[int, Fingering]  # ステップ → 運指
    tapped: set[int]                  # 右手タッピングで弾くステップ
    dropped: int                      # 押さえられず省いた音の数


def fit_range(pitch: int, opens: list[int], max_fret: int) -> int:
    """楽器の音域外の音をオクターブ移動して収める。"""
    lo, hi = min(opens), max(opens) + max_fret
    while pitch < lo:
        pitch += 12
    while pitch > hi:
        pitch -= 12
    return pitch


def enumerate_fingerings(pitches: list[int], opens: list[int],
                         max_fret: int, max_span: int) -> list[Fingering]:
    """和音を押さえられる (弦, フレット) の組み合わせをすべて列挙する。"""
    results: list[Fingering] = []

    def rec(i: int, used: set[int], cur: list[tuple[int, int]], lo: int, hi: int):
        if i == len(pitches):
            results.append(tuple(cur))
            return
        for s, open_pitch in enumerate(opens):
            if s in used:
                continue
            fret = pitches[i] - open_pitch
            if not 0 <= fret <= max_fret:
                continue
            nlo, nhi = lo, hi
            if fret > 0:  # 開放弦は左手の幅に数えない
                nlo, nhi = min(lo, fret), max(hi, fret)
                if nhi - nlo > max_span:
                    continue
            used.add(s)
            cur.append((s, fret))
            rec(i + 1, used, cur, nlo, nhi)
            cur.pop()
            used.remove(s)

    rec(0, set(), [], 99, 0)
    return results


def shape_cost(fing: Fingering) -> float:
    """押さえ方そのものの弾きにくさ。"""
    fretted = [f for _, f in fing if f > 0]
    if not fretted:
        return 0.0
    lo, hi = min(fretted), max(fretted)
    cost = 0.5 * (hi - lo) + 0.1 * hi  # 幅が狭く、ローポジションほど良い
    # ハイポジションで開放弦を混ぜる形は、音は出せても普通は選ばない
    cost += 0.4 * (len(fing) - len(fretted)) * max(0, lo - 4)
    fingers = len(fretted) - max(0, fretted.count(lo) - 1)  # 最低フレットはバレー可
    if fingers > 4:
        cost += 10.0
    return cost


def fret_range(fing: Fingering) -> tuple[int, int] | None:
    fretted = [f for _, f in fing if f > 0]
    return (min(fretted), max(fretted)) if fretted else None


def hand_positions(rng: tuple[int, int]) -> range:
    """rng のフレットを押さえられる左手ポジション(人差し指の位置)の範囲。"""
    lo, hi = rng
    return range(max(1, min(lo, hi - HAND_REACH)), lo + 1)


def playable_candidates(pitches: list[int], opens: list[int], max_fret: int,
                        max_span: int) -> tuple[list[Fingering], int]:
    """運指候補と、弾けないために省いた音の数を返す。"""
    dropped = 0
    if len(pitches) > len(opens):  # 弦の数より多い: 最低音 + 上の音を残す
        keep = len(opens)
        dropped = len(pitches) - keep
        pitches = [pitches[0]] + pitches[-(keep - 1):] if keep > 1 else pitches[-1:]
    n = len(pitches)
    for r in range(n):
        # 内声から優先して省く(最低音と最高音はなるべく残す)
        combos = sorted(itertools.combinations(range(n), r),
                        key=lambda c: ((n - 1) in c, 0 in c))
        for combo in combos:
            subset = [p for i, p in enumerate(pitches) if i not in combo]
            cands = enumerate_fingerings(subset, opens, max_fret, max_span)
            if cands:
                cands.sort(key=shape_cost)
                return cands[:MAX_CANDIDATES], dropped + r
    return [], dropped + n


def is_legato(prev: Event, cur: Event, tpb: int) -> bool:
    """前の音が切れずに次の音へつながっているか。"""
    return cur.on_tick - tpb / 8 <= prev.end_tick <= cur.on_tick + tpb / 4


def group_ringing(events: list[Event], opens: list[int], max_fret: int,
                  max_span: int, tpb: int) -> list[list[Event]]:
    """鳴らしっぱなしで重なっていく音(let ring のアルペジオ)をまとめる。

    まとめた音は同時に押さえている必要があるので、1つの和音として運指を決める。
    和音として押さえられない組み合わせになったら、そこで区切る。
    """
    groups = [[events[0]]]
    for prev, ev in zip(events, events[1:]):
        union = sorted({p for e in groups[-1] for p in e.ends} | set(ev.ends))
        shapes = (enumerate_fingerings(union, opens, max_fret, max_span)
                  if prev.min_end_tick > ev.on_tick + tpb / 4 and len(union) <= len(opens) else [])
        # 無理のないフォームで押さえられるときだけ、まとめて1つのコードとして扱う
        if shapes and min(map(shape_cost, shapes)) <= MAX_GROUP_COST:
            groups[-1].append(ev)
        else:
            groups.append([ev])
    return groups


def assign_fingerings(events: dict[int, Event], opens: list[int], grid: int,
                      max_fret: int, max_span: int, tpb: int = 480,
                      tapping: bool = False) -> Assignment:
    """曲全体で左手の移動が最小になるよう、各和音の運指を選ぶ(ビタビ)。

    tapping=True のとき、レガートでつながる単音は「左手を動かさず右手で
    タッピングする」選択肢も候補に入れる。
    """
    units: list[list[Event]] = []  # 運指を一緒に決めるイベントのまとまり
    cand_lists = []
    dropped_total = 0
    if not events:
        return Assignment({}, set(), 0)
    for group in group_ringing(list(events.values()), opens, max_fret, max_span, tpb):
        if len(group) > 1:
            union = sorted({p for e in group for p in e.ends})
            cands = sorted(enumerate_fingerings(union, opens, max_fret, max_span),
                           key=shape_cost)[:MAX_CANDIDATES]
            dropped = 0
        else:
            cands, dropped = playable_candidates(group[0].pitches, opens, max_fret, max_span)
        dropped_total += dropped
        if cands:
            units.append(group)
            cand_lists.append(cands)
    if not units:
        return Assignment({}, set(), dropped_total)

    def single(i: int) -> bool:
        return len(units[i]) == 1 and len(cand_lists[i][0]) == 1

    def legato_into(i: int) -> bool:
        return (0 < i < len(units) and single(i - 1) and single(i)
                and is_legato(units[i - 1][0], units[i][0], tpb))

    # 状態 = (累積コスト, 直前の状態index, 候補index, 左手ポジション, タッピングか)
    # ポジションは「まだ決まっていない」場合 None (開放弦だけで始まる曲など)
    layers: list[list[tuple[float, int, int, int | None, bool]]] = []
    for i, cands in enumerate(cand_lists):
        if i == 0:
            prev_best = {None: (0.0, -1)}
            move_weight = 0.0
        else:
            prev_best = {}  # ポジションごとの最良の直前状態
            for k, (cost, _, _, pos, _) in enumerate(layers[-1]):
                if pos not in prev_best or cost < prev_best[pos][0]:
                    prev_best[pos] = (cost, k)
            gap_beats = (units[i][0].step - units[i - 1][-1].step) * 4 / grid
            move_weight = 1.0 / (1.0 + gap_beats / 2)  # 間が空くほど移動は楽
        can_tap = tapping and (legato_into(i) or legato_into(i + 1))
        layer = []
        for ci, cand in enumerate(cands):
            sc = shape_cost(cand)
            rng = fret_range(cand)
            if rng is None:  # 開放弦だけなら手は動かない
                layer.extend((pcost + sc, k, ci, ppos, False)
                             for ppos, (pcost, k) in prev_best.items())
                continue
            for pos in hand_positions(rng):
                best = None
                for ppos, (pcost, k) in prev_best.items():
                    dist = abs(pos - ppos) if ppos is not None else 0
                    # 移動距離に加え、ポジション移動そのものにも固定コスト
                    total = pcost + (dist + 1.0 if dist else 0.0) * move_weight
                    if best is None or total < best[0]:
                        best = (total, k)
                layer.append((best[0] + sc, best[1], ci, pos, False))
            if can_tap:  # 左手より上のフレットを右手で叩く。左手は動かない
                fret = rng[0]
                layer.extend((pcost + TAP_COST + 0.1 * fret, k, ci, ppos, True)
                             for ppos, (pcost, k) in prev_best.items()
                             if ppos is not None and fret > ppos + HAND_REACH)
        layers.append(layer)

    j = min(range(len(layers[-1])), key=lambda k: layers[-1][k][0])
    result: dict[int, Fingering] = {}
    tapped = set()
    for i in range(len(units) - 1, -1, -1):
        _, prev_j, ci, _, tap = layers[i][j]
        for ev in units[i]:  # まとまりの運指から、各イベントで弾く弦だけ取り出す
            result[ev.step] = tuple((st, f) for st, f in cand_lists[i][ci]
                                    if opens[st] + f in ev.ends)
        if tap:
            tapped.add(units[i][0].step)
        j = prev_j
    return Assignment(dict(sorted(result.items())), tapped, dropped_total)


def detect_techniques(events: dict[int, Event], asg: Assignment,
                      tpb: int) -> dict[int, tuple[str, int | None]]:
    """レガートでつながる同一弦の単音を H(ハンマリング) / P(プリング) とみなす。

    戻り値: ステップ → (記号 'H'|'P'|'T', スラーでつなぐ直前のステップ or None)
    """
    techs: dict[int, tuple[str, int | None]] = {s: ("T", None) for s in asg.tapped}
    steps = list(asg.fingerings)
    for a, b in zip(steps, steps[1:]):
        fa, fb = asg.fingerings[a], asg.fingerings[b]
        if len(fa) != 1 or len(fb) != 1 or fa[0][0] != fb[0][0] or fa[0][1] == fb[0][1]:
            continue
        if not is_legato(events[a], events[b], tpb):
            continue
        if b in asg.tapped:
            techs[b] = ("T", a)
        else:
            techs[b] = ("H" if fb[0][1] > fa[0][1] else "P", a)
    return techs


# ---------------------------------------------------------------- リズム

@dataclass(frozen=True)
class Dur:
    """音価。k: 全音符=0, 2分=1, 4分=2, 8分=3, 16分=4, 32分=5"""
    k: int
    dotted: bool = False
    triplet: bool = False


@dataclass
class Item:
    """リズム上の1要素(音符または休符)。"""
    start: int
    length: int
    dur: Dur
    step: int | None       # 対応するEventのステップ。休符は None
    tied: bool = False     # 直前の音符からタイでつながる
    whole_rest: bool = False  # 1小節まるごとの休符


def dur_table(grid: int) -> list[tuple[int, int, Dur]]:
    """このグリッドで書ける音価: (ステップ数, 置ける位置の単位, Dur) を長い順に。"""
    table: dict[int, tuple[int, int, Dur]] = {}
    for k in range(6):
        if grid % (1 << k) == 0:
            base = grid >> k
            table.setdefault(base, (base, base, Dur(k)))
            if base % 2 == 0:
                table.setdefault(base * 3 // 2, (base * 3 // 2, base, Dur(k, dotted=True)))
    for k in range(1, 6):
        if (2 * grid) % (3 << k) == 0:
            n = 2 * grid // (3 << k)
            table.setdefault(n, (n, n, Dur(k, triplet=True)))
    return sorted(table.values(), reverse=True)


def decompose(pos: int, length: int, mstart: int, table) -> list[tuple[int, int, Dur]]:
    """[pos, pos+length) を書ける音価の並びに分解する。"""
    out = []
    while length > 0:
        for steps, align, dur in table:
            if steps <= length and (pos - mstart) % align == 0:
                break
        else:
            steps, dur = 1, Dur(4)
        out.append((pos, steps, dur))
        pos += steps
        length -= steps
    return out


def nice_length(sound: float, pos: int, grid: int, table) -> int:
    """鳴っている長さ(ステップ数)を、1つの音符で書ける長さに丸める。

    3連符の音価は、3連符の位置で始まる音にだけ使う。
    """
    plain_unit = grid // 16 if grid % 16 == 0 else 0
    on_plain = plain_unit and pos % plain_unit == 0
    choices = [steps for steps, _, dur in table if not (dur.triplet and on_plain)]
    return min(choices, key=lambda n: (abs(n - sound), n))


def build_rhythm(events: dict[int, Event], fingerings: dict[int, Fingering],
                 measures: list[Measure], grid: int, tpb: int) -> tuple[int, list[list[Item]]]:
    """音の長さから、小節ごとの音符・休符・タイの並びを作る。

    単旋律のリズムとして書く: 各音は次の発音まで伸ばし、明らかに短く切れて
    いる場合だけ休符を入れる。戻り値は (最初の小節index, 小節ごとのItem)。
    """
    step_ticks = tpb * 4 / grid
    table = dur_table(grid)
    steps = sorted(fingerings)
    first = next(i for i, m in enumerate(measures) if m[0] + m[1] > steps[0])
    end = measures[-1][0] + measures[-1][1]
    segs: list[tuple[int, int, int | None]] = []
    cursor = measures[first][0]
    for i, s in enumerate(steps):
        if s > cursor:
            segs.append((cursor, s, None))
        sound = events[s].end_tick - s * step_ticks
        if i + 1 < len(steps):
            gap = steps[i + 1] - s
            n = gap if sound >= 0.6 * gap * step_ticks else \
                min(gap, nice_length(sound / step_ticks, s, grid, table))
        else:
            n = min(end - s, nice_length(sound / step_ticks, s, grid, table))
        segs.append((s, s + n, s))
        cursor = s + n
    if cursor < end:
        segs.append((cursor, end, None))

    out: list[list[Item]] = []
    for ms, ml, _, _ in measures[first:]:
        items = []
        for a, b, step in segs:
            lo, hi = max(a, ms), min(b, ms + ml)
            if lo >= hi:
                continue
            if step is None and lo == ms and hi == ms + ml:
                items.append(Item(lo, ml, Dur(0), None, whole_rest=True))
                continue
            for j, (pos, n, dur) in enumerate(decompose(lo, hi - lo, ms, table)):
                items.append(Item(pos, n, dur, step,
                                  tied=step is not None and (lo > a or j > 0)))
        out.append(items)
    return first, out


def find_ringing(events: dict[int, Event], steps: list[int], grid: int, tpb: int) -> set[int]:
    """次の音が鳴ったあとも伸び続ける音(let ring)を持つステップ。"""
    step_ticks = tpb * 4 / grid
    return {a for a, b in zip(steps, steps[1:])
            if events[a].end_tick > b * step_ticks + tpb / 4}


# ---------------------------------------------------------------- 打ち込み特有の処理

def collapse_glissandi(song: Song) -> dict[int, str]:
    """半音ずつ素早く上下する音の連なり(打ち込みのスライド効果)を1つの記号にまとめる。

    声部ごとに「前の音が終わると同時に半音隣のごく短い音が始まる」連なりを探す。
    該当する音は song.notes から取り除き、元の音を連なりの終わりまで伸ばす。
    戻り値: 元の音のtick → "so_down" / "so_up"
    """
    tpb = song.ticks_per_beat
    short, join = tpb / 10, tpb / 16
    notes = sorted(song.notes, key=lambda n: n.on)
    by_pitch: dict[int, list[Note]] = collections.defaultdict(list)
    for n in notes:
        by_pitch[n.pitch].append(n)
    removed: set[int] = set()
    out: dict[int, str] = {}

    def follower(a: Note, d: int) -> Note | None:
        for b in by_pitch.get(a.pitch + d, ()):
            if (id(b) not in removed and b.on > a.on and abs(b.on - a.off) <= join
                    and b.off - b.on <= short):
                return b
        return None

    for a in notes:
        if id(a) in removed:
            continue
        for d in (-1, 1):
            chain = []
            cur = a
            while (b := follower(cur, d)) is not None:
                chain.append(b)
                cur = b
            if len(chain) >= 4:
                removed.update(id(n) for n in chain)
                a.off = max(a.off, chain[-1].off)
                out[a.on] = "so_down" if d < 0 else "so_up"
                break
    song.notes = [n for n in song.notes if id(n) not in removed]
    return out


def collapse_grace_notes(song: Song) -> dict[int, str]:
    """直後の音へ半音〜全音で滑り込むごく短い音を、スライドインの記号にまとめる。

    戻り値: 滑り込んだ先の音のtick → "si_up"(下から) / "si_down"(上から)
    """
    tpb = song.ticks_per_beat
    by_pitch: dict[int, list[Note]] = collections.defaultdict(list)
    for n in song.notes:
        by_pitch[n.pitch].append(n)
    removed: set[int] = set()
    out: dict[int, str] = {}
    for a in sorted(song.notes, key=lambda n: n.on):
        length = a.off - a.on
        if length > tpb / 8:
            continue
        for d in (1, -1, 2, -2):
            target = next((b for b in by_pitch.get(a.pitch + d, ())
                           if b.on > a.on and abs(b.on - a.off) <= tpb / 24
                           and b.off - b.on >= 2 * length and id(b) not in removed), None)
            if target is not None:
                removed.add(id(a))
                out[target.on] = "si_up" if d > 0 else "si_down"
                break
    song.notes = [n for n in song.notes if id(n) not in removed]
    return out


def add_dead_strums(song: Song, events: dict[int, Event], asg: Assignment,
                    opens: list[int], step_ticks: float) -> set[int]:
    """空ピッキングを、直前のコードと同じ弦の X として譜面に加える。"""
    real = sorted(asg.fingerings)
    added: set[int] = set()
    for tick in song.dead:
        step = round(tick / step_ticks)
        if step in asg.fingerings or not real:
            continue
        i = bisect.bisect_left(real, step)
        # 近くにコード(3音以上)があればその形、なければ直前の音の弦を使う
        near = [s for s in real[max(0, i - 8):i] if len(asg.fingerings[s]) >= 3]
        ref = near[-1] if near else real[i - 1] if i else real[0]
        fing = asg.fingerings[ref]
        asg.fingerings[step] = fing
        events[step] = Event(step, tick, {opens[s] + f: tick + round(step_ticks) for s, f in fing},
                             events[ref].channel)
        added.add(step)
    if added:
        asg.fingerings = dict(sorted(asg.fingerings.items()))
        ordered = dict(sorted(events.items()))
        events.clear()
        events.update(ordered)
    return added


def find_sections(song: Song, measures: list[Measure], step_ticks: float) -> dict[int, str]:
    """MIDIのマーカー(Verse, Chorus など)を、いちばん近い小節の頭に割り当てる。"""
    starts = [m[0] for m in measures]
    out: dict[int, str] = {}
    for tick, text in song.markers:
        step = tick / step_ticks
        i = min(range(len(starts)), key=lambda k: abs(starts[k] - step))
        if text and abs(starts[i] - step) <= measures[i][1] / 2:
            out[i] = text
    return out


# ---------------------------------------------------------------- 解析全体

@dataclass
class Score:
    opens: list[int]
    capo: int
    grid: int
    bpm: float | None
    events: dict[int, Event]
    measures: list[Measure]
    fingerings: dict[int, Fingering]
    tapped: set[int]
    techs: dict[int, tuple[str, int | None]]
    ringing: set[int]
    chords: dict[int, str]  # ステップ → コードネーム
    first_measure: int
    rhythm: list[list[Item]]
    warnings: list[str]
    tpb: int = 480
    max_fret: int = 20
    marks: dict[int, list[str]] = field(default_factory=dict)  # ステップ → マーク
    sections: dict[int, str] = field(default_factory=dict)     # 小節index → セクション名

    def tick(self, step: int) -> int:
        """ステップ番号 → tick。編集内容のキーに使う(グリッドを変えても同じ位置を指す)。"""
        return round(step * self.tpb * 4 / self.grid)


def analyze(mid: mido.MidiFile, *, tuning: str = "standard", capo: int = 0,
            grid: int | None = None, tracks: list[int] | None = None,
            channels: list[int] | None = None, transpose: int = 0,
            max_fret: int = 20, max_span: int = 4,
            legato: bool = False, tapping: bool = False,
            chords: bool = True, expression: bool = True,
            dead_notes: list[int] | None = None,
            edits: dict | None = None) -> Score:
    warnings: list[str] = []
    opens = [p + capo for p in parse_tuning(tuning)]
    max_fret -= capo
    song = load_song(mid, tracks, channels, transpose, frozenset(dead_notes or ()))
    if not song.notes:
        raise ValueError("ノートが見つかりません(トラック/チャンネル指定を確認してください)")
    tpb = song.ticks_per_beat
    glides = collapse_glissandi(song) if expression else {}
    if expression:
        glides.update(collapse_grace_notes(song))
    # 一瞬だけ鳴る音は演奏ノイズとみなす
    tiny = [n for n in song.notes if n.off - n.on < tpb / 32]
    if tiny and len(tiny) < len(song.notes):
        song.notes = [n for n in song.notes if n.off - n.on >= tpb / 32]
        warnings.append(f"ごく短い {len(tiny)} 音を無視しました")

    # 音域より高い音は弾けない(音源のキースイッチであることが多い)ので無視する。
    # 低すぎる音はオクターブ上げて収める
    top = max(opens) + max_fret
    too_high = collections.Counter(n.pitch for n in song.notes if n.pitch > top)
    song.notes = [n for n in song.notes if n.pitch <= top]
    if too_high:
        names = ", ".join(f"{pitch_class_name(p)}{p // 12 - 1}(ノート番号{p})×{c}"
                          for p, c in sorted(too_high.items()))
        warnings.append(f"音域より高い {sum(too_high.values())} 音を無視しました: {names}。"
                        "空ピッキング用のキーなら --dead-notes で X として表示できます")
    if not song.notes:
        raise ValueError("このチューニングで弾ける音がありません")
    shifted = 0
    for n in song.notes:
        new = fit_range(n.pitch, opens, max_fret)
        shifted += new != n.pitch
        n.pitch = new
    if shifted:
        warnings.append(f"音域より低い {shifted} 音をオクターブ上げました")

    if grid is None:
        grid = choose_grid(song)
    events = quantize(song, grid)
    asg = assign_fingerings(events, opens, grid, max_fret, max_span, tpb, tapping)
    if asg.dropped:
        warnings.append(f"同時に押さえられない {asg.dropped} 音を省略しました")
    if not asg.fingerings:
        raise ValueError("このチューニングで弾ける音がありません")

    step_ticks = tpb * 4 / grid
    dead_steps = add_dead_strums(song, events, asg, opens, step_ticks)
    if edits:
        apply_note_edits(asg, opens, max_fret, edits, step_ticks)
        if not asg.fingerings:
            raise ValueError("すべての音が非表示になっています")
    last = max(max(events), round(max(e.end_tick for e in events.values()) / step_ticks) - 1)
    measures = build_measures(song, grid, last)
    first, rhythm = build_rhythm(events, asg.fingerings, measures, grid, tpb)
    techs = detect_techniques(events, asg, tpb) if (legato or tapping) else {}
    ringing = find_ringing(events, list(asg.fingerings), grid, tpb)
    sounding = {s: e for s, e in events.items() if s not in dead_steps}
    names = chordlib.detect_chords(sounding, measures[first:], grid, tpb) if chords else {}
    marks = marklib.detect_expression(song, events) if expression else {}
    for tick, code in glides.items():
        marks.setdefault(round(tick / step_ticks), []).append(code)
    for s in dead_steps:
        marks[s] = ["x"]
    if edits:
        apply_mark_edits(techs, names, asg.fingerings, edits, step_ticks)
        for key, codes in edits.get("marks", {}).items():
            marks[round(int(key) / step_ticks)] = marklib.clean_marks(codes)
    marks = {s: m for s, m in marks.items() if m and s in asg.fingerings}
    return Score(opens, capo, grid, song.bpm, events, measures, asg.fingerings,
                 asg.tapped, techs, ringing, names, first, rhythm, warnings, tpb, max_fret, marks,
                 find_sections(song, measures, step_ticks))


# ---------------------------------------------------------------- 手動編集
#
# 編集内容は JSON で持つ。位置は tick で指す:
#   {"title": "曲名",
#    "options": {"tuning": "dropd", "capo": 2, "grid": 16, "legato": true, ...},
#    "notes":  {"<tick>:<音高>": {"string": 2} または {"hide": true}},
#    "techs":  {"<tick>": "H" | "P" | "T" | "sl" | "S" | ""},   "" は自動判定の取り消し
#    "marks":  {"<tick>": ["vib", "b2", "pm", ...]},   その音のマークを丸ごと指定 (marks.py 参照)
#    "chords": {"<tick>": "Am7" | ""}}                 "" は自動判定の取り消し

EDITABLE_OPTIONS = {"tuning": "standard", "capo": 0, "grid": None,
                    "legato": False, "tapping": False, "chords": True, "expression": True,
                    "dead_notes": None}


def apply_note_edits(asg: Assignment, opens: list[int], max_fret: int,
                     edits: dict, step_ticks: float) -> None:
    """弦の変更・音の非表示を運指に反映する。成立しない指定は無視する。"""
    for key, edit in edits.get("notes", {}).items():
        try:
            tick, pitch = (int(x) for x in key.split(":"))
        except ValueError:
            continue
        step = round(tick / step_ticks)
        fing = asg.fingerings.get(step)
        if not fing or not any(opens[s] + f == pitch for s, f in fing):
            continue
        others = [(s, f) for s, f in fing if opens[s] + f != pitch]
        if edit.get("hide"):
            new = others
        else:
            s = edit.get("string")
            if not isinstance(s, int) or not 0 <= s < len(opens):
                continue
            fret = pitch - opens[s]
            if not 0 <= fret <= max_fret or any(o == s for o, _ in others):
                continue
            new = others + [(s, fret)]
        if new:
            asg.fingerings[step] = tuple(sorted(new))
        else:
            del asg.fingerings[step]
            asg.tapped.discard(step)


def apply_mark_edits(techs: dict, names: dict[int, str], fingerings: dict[int, Fingering],
                     edits: dict, step_ticks: float) -> None:
    """奏法記号とコードネームの手動指定を反映する。"""
    steps = list(fingerings)
    for key, label in edits.get("techs", {}).items():
        step = round(int(key) / step_ticks)
        if step not in fingerings:
            continue
        if label not in marklib.CONNECTIONS:
            techs.pop(step, None)
            continue
        i = steps.index(step)
        prev = steps[i - 1] if i > 0 else None
        linked = (prev is not None and len(fingerings[prev]) == 1 and len(fingerings[step]) == 1
                  and fingerings[prev][0][0] == fingerings[step][0][0])
        techs[step] = (label, prev if linked else None)
    for key, name in edits.get("chords", {}).items():
        step = round(int(key) / step_ticks)
        if name:
            names[step] = str(name)[:24]
        else:
            names.pop(step, None)
    for step in [s for s in techs if s not in fingerings]:
        del techs[step]


def load_edits(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"編集ファイルの形式が不正です: {path}")
    return data


def analyze_with_edits(mid: mido.MidiFile, fixed: dict, edits: dict) -> Score:
    """edits["options"] の設定と手動編集を反映して解析する。"""
    saved = edits.get("options", {})
    options = {k: saved.get(k, default) for k, default in EDITABLE_OPTIONS.items()}
    return analyze(mid, **fixed, **options, edits=edits)


# ---------------------------------------------------------------- ASCII描画

def render_tab(fingerings: dict[int, Fingering], measures: list[Measure],
               opens: list[int], width: int = 100) -> str:
    n_strings = len(opens)
    max_digits = max((len(str(f)) for fing in fingerings.values() for _, f in fing),
                     default=1)
    cell = max_digits + 1
    labels = [pitch_class_name(p) for p in opens]
    label_w = max(len(x) for x in labels)

    # 最初の音が鳴る小節から始める
    first = min(fingerings) if fingerings else 0
    start_idx = next(i for i, m in enumerate(measures) if m[0] + m[1] > first)

    blocks = []  # (小節番号, 弦ごとの文字列[高音弦が先頭])
    for idx in range(start_idx, len(measures)):
        start, length = measures[idx][:2]
        rows = [["-"] for _ in range(n_strings)]
        for step in range(start, start + length):
            frets = dict(fingerings.get(step, ()))
            for s in range(n_strings):
                text = str(frets[s]) if s in frets else ""
                rows[s].append(text.ljust(cell, "-"))
        blocks.append((idx + 1, ["".join(r) + "|" for r in reversed(rows)]))

    lines = []
    i = 0
    while i < len(blocks):
        line_blocks = [blocks[i]]
        used = label_w + 1 + len(blocks[i][1][0])
        i += 1
        while i < len(blocks) and used + len(blocks[i][1][0]) <= width:
            used += len(blocks[i][1][0])
            line_blocks.append(blocks[i])
            i += 1
        lines.append(" " * (label_w + 1) + f"[{line_blocks[0][0]}]")
        for row in range(n_strings):
            label = labels[n_strings - 1 - row].ljust(label_w)
            lines.append(label + "|" + "".join(b[1][row] for b in line_blocks))
        lines.append("")
    return "\n".join(lines)


def render_ascii(score: Score, width: int = 100) -> str:
    header = [f"Tuning: {' '.join(pitch_class_name(p) for p in score.opens)} (低音弦→高音弦)"
              + (f"  Capo: {score.capo}" if score.capo else "")]
    _, _, num, den = score.measures[0]
    info = f"拍子: {num}/{den}  1マス = {score.grid}分音符"
    if score.bpm:
        info += f"  テンポ: {score.bpm:.0f} BPM"
    header.append(info)
    return "\n".join(header) + "\n\n" + render_tab(
        score.fingerings, score.measures, score.opens, width)


def transcribe(mid: mido.MidiFile, *, width: int = 100,
               warnings: list[str] | None = None, **options) -> str:
    """MIDIをASCIIのTAB譜にする(options は analyze と同じ)。"""
    score = analyze(mid, **options)
    if warnings is not None:
        warnings.extend(score.warnings)
    return render_ascii(score, width)


# ---------------------------------------------------------------- CLI

def int_list(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ギターのMIDIファイルをTAB譜に書き起こす")
    ap.add_argument("midi", help="入力MIDIファイル")
    ap.add_argument("-o", "--output", help="出力ファイル。.html / .pdf ならリズム表記つきの譜面、それ以外はASCII")
    ap.add_argument("--format", choices=["ascii", "html", "pdf"], help="出力形式(既定は -o の拡張子で判断)")
    ap.add_argument("--edit", action="store_true", help="ブラウザで編集画面を開く")
    ap.add_argument("--edits", help="編集内容のJSONファイル(既定は <MIDIファイル名>.edits.json)")
    ap.add_argument("--port", type=int,
                    help="編集画面のポート番号 (既定は 8765、使えなければ空き番号を自動で選ぶ)")
    ap.add_argument("--no-open", action="store_true", help="編集画面を起動してもブラウザを開かない")
    ap.add_argument("--title", help="譜面のタイトル(HTML出力。既定はファイル名)")
    ap.add_argument("--list", action="store_true", help="トラック一覧を表示して終了")
    ap.add_argument("--track", type=int_list, help="使うトラック番号(カンマ区切り)。既定は全トラック")
    ap.add_argument("--channel", type=int_list, help="使うMIDIチャンネル 1-16(カンマ区切り)。既定はドラム以外")
    ap.add_argument("--tuning",
                    help=f"プリセット({', '.join(TUNINGS)}) または 'E2 A2 D3 G3 B3 E4' 形式")
    ap.add_argument("--capo", type=int, help="カポの位置")
    ap.add_argument("--grid", type=int, help="1マスの音価 (8, 16, 12=3連8分 など)。既定は自動判定")
    ap.add_argument("--transpose", type=int, default=0, help="半音単位の移調")
    ap.add_argument("--max-fret", type=int, default=20, help="最高フレット (既定 20)")
    ap.add_argument("--max-span", type=int, default=4, help="左手が届くフレット幅 (既定 4)")
    ap.add_argument("--legato", action=argparse.BooleanOptionalAction,
                    help="音が切れずにつながる同一弦の音を H(ハンマリング)/P(プリング) と推定する")
    ap.add_argument("--tapping", action=argparse.BooleanOptionalAction,
                    help="左手が届かないレガートの音を T(タッピング) と推定する (--legato を含む)")
    ap.add_argument("--dead-notes", type=int_list,
                    help="空ピッキング(ブラッシング)を表すノート番号(カンマ区切り)。X として表示する")
    ap.add_argument("--expression", action=argparse.BooleanOptionalAction,
                    help="ピッチベンドからチョーキング・ビブラートを推定 (既定で有効)")
    ap.add_argument("--chords", action=argparse.BooleanOptionalAction,
                    help="コードネームの自動判定 (既定で有効。--no-chords で無効)")
    ap.add_argument("--width", type=int, default=100, help="ASCII出力の1行の最大文字数 (既定 100)")
    args = ap.parse_args(argv)

    try:
        mid = mido.MidiFile(args.midi)
    except (OSError, ValueError, EOFError) as e:
        print(f"MIDIファイルを読めません: {e}", file=sys.stderr)
        return 1

    if args.list:
        for t in scan_tracks(mid):
            rng = (f"{pitch_class_name(t.low)}{t.low // 12 - 1}-"
                   f"{pitch_class_name(t.high)}{t.high // 12 - 1}") if t.notes else "-"
            chs = ",".join(str(c + 1) for c in sorted(t.channels)) or "-"
            name = t.name or "(無名)"
            if t.instrument and t.instrument != t.name:
                name += f" / {t.instrument}"
            span = (f"{t.first_tick / mid.ticks_per_beat:.0f}-{t.last_tick / mid.ticks_per_beat:.0f}拍"
                    if t.notes else "-")
            print(f"track {t.index:>2}: {name:<26} notes={t.notes:<5} ch={chs:<4} 音域={rng:<8} 位置={span}")
        print("位置が重ならないトラックは曲の区間ごとの断片なので、--track を付けずに全部まとめて変換する")
        return 0

    out_name = (args.output or "").lower()
    fmt = args.format or ("html" if out_name.endswith((".html", ".htm"))
                          else "pdf" if out_name.endswith(".pdf") else "ascii")
    edits_path = args.edits or args.midi + ".edits.json"
    try:
        edits = load_edits(edits_path)
    except (OSError, ValueError) as e:
        print(f"編集ファイルを読めません: {e}", file=sys.stderr)
        return 1
    # コマンドラインで明示した設定は、保存済みの設定より優先する
    options = dict(edits.get("options", {}))
    for key in EDITABLE_OPTIONS:
        if getattr(args, key) is not None:
            options[key] = getattr(args, key)
    edits["options"] = options
    fixed = dict(tracks=args.track,
                 channels=[c - 1 for c in args.channel] if args.channel else None,
                 transpose=args.transpose, max_fret=args.max_fret, max_span=args.max_span)
    default_title = os.path.splitext(os.path.basename(args.midi))[0]
    if args.title:
        edits["title"] = args.title

    if args.edit:
        import editor
        return editor.serve(mid, fixed, edits, edits_path, default_title,
                            port=args.port, open_browser=not args.no_open)

    try:
        score = analyze_with_edits(mid, fixed, edits)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    for w in score.warnings:
        print(f"注意: {w}", file=sys.stderr)
    title = edits.get("title") or default_title
    if fmt == "pdf":
        import pdfout
        import tabsvg
        if not args.output:
            print("エラー: PDF出力には -o で出力ファイルを指定してください", file=sys.stderr)
            return 1
        try:
            data = pdfout.html_to_pdf(tabsvg.render_html(score, title))
        except pdfout.PdfError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        with open(args.output, "wb") as f:
            f.write(data)
        return 0
    if fmt == "html":
        import tabsvg
        text = tabsvg.render_html(score, title)
    else:
        text = render_ascii(score, args.width)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
