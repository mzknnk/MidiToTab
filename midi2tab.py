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
INVERT_COST = 1.0       # 低音弦側が高音弦側より3フレット以上高い形の、超過1フレットあたりのコスト
SKIP_COST = 2.0         # 同時に弾く音の間で、余分に飛ばす弦1本あたりのコスト
RING_CUT = 1.0          # 鳴っている音と同じ弦を使って切ってしまうコスト(切れる長さ1拍あたり)
SAME_SHAPE_MOVE = 0.25 # 同じフォームのまま1フレット動かすコスト(通常の移動は 1 + 固定1)
TAP_STRINGS = 4      # 右手で叩くのは高音側のこの本数の弦だけ(5・6弦は叩かない)
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
    tempos: list[tuple[int, int]] = field(default_factory=list)   # (tick, 1拍のマイクロ秒)


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
    tempos: list[tuple[int, int]] = []
    for i, track in enumerate(mid.tracks):
        tick = 0
        sounding: dict[tuple[int, int], list[Note]] = {}
        for msg in track:
            tick += msg.time
            if msg.type == "time_signature":
                time_sigs.append((tick, msg.numerator, msg.denominator))
            elif msg.type == "set_tempo":
                tempos.append((tick, msg.tempo))
                if bpm is None:
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
    return Song(mid.ticks_per_beat, notes, time_sigs, bpm, wheel, mod, dead, sorted(markers),
                sorted(tempos))


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


def skip_cost(notes) -> float:
    """同時に弾く音の間で、必要以上に弦を飛ばす弾きにくさ。notes: (弦, 音高) の並び。

    音程に対して自然な弦の間隔(5度までは隣の弦、オクターブ前後は1本飛ばし)より
    多く飛ばすと、間の弦を余分にミュートしなければならない。ベースとメロディのように
    もともと音程が広い組み合わせは、弦が離れていても減点しない。
    """
    used = sorted(set(notes))
    cost = 0.0
    for (a, pa), (b, pb) in zip(used, used[1:]):
        gap = b - a - 1
        interval = abs(pb - pa)
        natural = 0 if interval <= 7 else 1 if interval <= 14 else 2 if interval <= 19 else 9
        if gap > natural:
            cost += SKIP_COST * (gap - natural)
    return cost


def inversion(fing) -> int:
    """低音弦側の指が、高音弦側の指より何フレット上(ブリッジ寄り)にあるか。

    手の構造上、高音弦ほど上のフレットへ伸ばすのは楽だが、その逆(低音弦だけ高いフレット)は
    難しい。Cコードのような2フレットまでの逆向きは普通なので、それを超えた分が問題になる。
    """
    fretted = sorted((s, f) for s, f in fing if f > 0)
    worst = 0
    lowest_seen = None  # ここまでの低音弦側で一番高いフレット
    for _, fret in fretted:
        if lowest_seen is not None:
            worst = max(worst, lowest_seen - fret)
        lowest_seen = fret if lowest_seen is None else max(lowest_seen, fret)
    return worst


def shape_cost(fing: Fingering, simultaneous: bool = True) -> float:
    """押さえ方そのものの弾きにくさ。simultaneous=False は、音が同時には鳴らない
    (順に押さえていく)まとまり用で、指の本数は数えない。"""
    fretted = [f for _, f in fing if f > 0]
    if not fretted:
        return 0.0
    lo, hi = min(fretted), max(fretted)
    cost = 0.5 * (hi - lo) + 0.1 * hi  # 幅が狭く、ローポジションほど良い
    cost += 0.05 * len(fretted)        # 同じなら押さえる音が少ない(開放弦を使う)ほうを選ぶ
    cost += INVERT_COST * max(0, inversion(fing) - 2)  # 低音弦だけ高いフレットになる形
    # ハイポジションで開放弦を混ぜる形は、音は出せても普通は選ばない
    cost += 0.4 * (len(fing) - len(fretted)) * max(0, lo - 4)
    fingers = len(fretted) - max(0, fretted.count(lo) - 1)  # 最低フレットはバレー可
    if simultaneous and fingers > 4:
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


@dataclass(frozen=True)
class Cand:
    """運指の候補。右手で叩く音がある場合、左手のフォームは left だけで決まる。"""
    fing: Fingering                  # すべての音
    left: Fingering                  # 左手で押さえる音
    taps: frozenset = frozenset()    # 右手で叩く (弦, フレット)
    cost: float = 0.0                # 弾きにくさ(タッピングの手間を含む)
    shape: float = 0.0               # 左手のフォームだけの弾きにくさ


def shape_signature(cand: Cand):
    """左手のフォームの「形」。同じ形なら、位置が違っても平行移動で弾ける。"""
    fretted = [(s, f) for s, f in cand.left if f > 0]
    if len(fretted) < 2:
        return None
    lo = min(f for _, f in fretted)
    return (tuple(sorted((s, f - lo) for s, f in fretted)),
            tuple(sorted(s for s, f in cand.left if f == 0)))


def plain_cands(fingerings: list[Fingering]) -> list[Cand]:
    return [Cand(f, f, frozenset(), shape_cost(f), shape_cost(f)) for f in fingerings]


MAX_GROUP_PITCHES = 8   # 1つのまとまりで扱う音高の数の上限
BEAM = 60               # まとまりの運指候補を何通りまで追うか


class GroupBuilder:
    """音が重なって鳴り続けるイベントのまとまりと、その運指候補を育てる。

    候補は「音高 → (弦, フレット, 右手で叩くか)」の割り当て。同時に弾く音どうしは
    必ず別の弦に置き、左手の音は1つのフォーム(max_span 以内)に収める。
    前の音がまだ鳴っている弦で次の音を弾くと前の音が切れるので、それは減点にとどめる
    (鳴らしっぱなしのアルペジオは別々の弦が選ばれやすく、同じ形で動くオクターブ奏法などは
    同じ弦を使える)。重ならない音は自由に同じ弦を使ってよい。
    """

    def __init__(self, opens: list[int], max_fret: int, max_span: int, tpb: int, tapping: bool,
                 tap_steps: set[int] | None = None):
        self.opens, self.max_fret, self.max_span = opens, max_fret, max_span
        self.tpb, self.tapping, self.tap_steps = tpb, tapping, tap_steps
        self.events: list[Event] = []
        self.spans: dict[int, list[tuple[int, int]]] = {}   # 音高 → 鳴っている区間
        self.beam: list[tuple[float, float, dict[int, tuple[int, int, bool]]]] = []
        self.hard: set[frozenset] = set()   # 同時に弾く音の組(同じ弦には置けない)
        # 鳴っている間に次が始まる音の組 → 同じ弦に置くと切れてしまう長さ(拍)
        self.soft: dict[frozenset, float] = {}

    def start(self, ev: Event) -> bool:
        """ev から新しいまとまりを始める。和音として押さえられなければ False。"""
        self.events, self.spans, self.beam = [], {}, [(0.0, 0.0, {})]
        self.hard, self.soft = set(), {}
        return self.add(ev)

    def overlap_beats(self, p: int, q: int) -> float:
        """p と q が重なって鳴る長さ(拍)。重なりとみなせないほど短ければ 0。"""
        longest = 0.0
        for a0, a1 in self.spans[p]:
            for b0, b1 in self.spans[q]:
                overlap = min(a1, b1) - max(a0, b0)
                if overlap > 0 and overlap >= min(self.tpb / 8, (a1 - a0) / 2, (b1 - b0) / 2):
                    longest = max(longest, overlap / self.tpb)
        return min(longest, 2.0)

    def score(self, places: dict[int, tuple[int, int, bool]]) -> tuple[float, float] | None:
        left = tuple((s, f) for s, f, tap in places.values() if not tap)
        taps = [f for _, f, tap in places.values() if tap]
        low_strings = sum(len(self.opens) - 1 - s for s, _, tap in places.values() if tap)
        rng = fret_range(left)
        if rng and rng[1] - rng[0] > self.max_span:
            return None
        if taps and (rng is None or min(taps) < rng[1] + 2):
            return None  # 右手は左手のフォームより上を叩く
        shape = shape_cost(left, simultaneous=len(self.events) <= 1)
        # 同時に弾く音どうしの弦飛び
        shape += sum(skip_cost((places[p][0], p) for p in ev.ends if p in places)
                     for ev in self.events if len(ev.ends) > 1)
        # 鳴っている音と同じ弦を使って、その音を切ってしまう組
        shape += RING_CUT * sum(lost for pair, lost in self.soft.items()
                                if all(p in places for p in pair)
                                and len({places[p][0] for p in pair}) == 1)
        # 右手で叩くのは高音弦が普通。低音弦ほど不自然なので避ける
        return shape + TAP_COST * len(taps) + 0.05 * sum(taps) + 0.4 * low_strings, shape

    def add(self, ev: Event) -> bool:
        """ev をまとまりに加える。無理のない運指が残らなければ何も変えず False。"""
        old_spans = {p: list(v) for p, v in self.spans.items()}
        old_pairs = (set(self.hard), dict(self.soft))
        for p, end in ev.ends.items():
            self.spans.setdefault(p, []).append((ev.on_tick, end))
        for p in ev.ends:
            for q in self.spans:
                pair = frozenset((p, q))
                if q == p or pair in self.hard:
                    continue
                if q in ev.ends:
                    self.hard.add(pair)
                    self.soft.pop(pair, None)
                else:
                    lost = self.overlap_beats(p, q)
                    if lost:
                        self.soft[pair] = max(lost, self.soft.get(pair, 0.0))
        ok = len(self.spans) <= MAX_GROUP_PITCHES
        beam = self.beam if ok else []
        self.events.append(ev)
        for p in sorted(ev.ends):
            clash = [q for q in self.spans if frozenset((p, q)) in self.hard]
            grown = []
            for _, _, places in beam:
                if p in places:  # すでに場所が決まっている音: 新しく重なる音と弦がぶつからないか
                    if all(q not in places or places[q][0] != places[p][0] for q in clash):
                        grown.append(places)
                    continue
                busy = {places[q][0] for q in clash if q in places}
                for string, open_pitch in enumerate(self.opens):
                    fret = p - open_pitch
                    if string in busy or not 0 <= fret <= self.max_fret:
                        continue
                    grown.append({**places, p: (string, fret, False)})
                    # 右手で叩くのは単音だけ(同時に弾く和音の片方だけを叩く形にはしない)
                    if (self.tapping and len(self.events) > 1 and fret > 0 and len(ev.ends) == 1
                            and string >= len(self.opens) - TAP_STRINGS
                            and (self.tap_steps is None or ev.step in self.tap_steps)):
                        grown.append({**places, p: (string, fret, True)})
            scored = []
            for places in grown:
                sc = self.score(places)
                if sc:
                    scored.append((sc[0], sc[1], places))
            scored.sort(key=lambda c: c[0])
            beam = scored[:BEAM]
        if beam and (len(self.events) == 1 or min(c[1] for c in beam) <= MAX_GROUP_COST):
            self.beam = beam
            return True
        self.events.pop()
        self.spans = old_spans
        self.hard, self.soft = old_pairs
        return False

    def candidates(self) -> list[Cand]:
        out = []
        # 複数のイベントをまとめた場合、無理のあるフォームは候補に残さない
        limit = MAX_GROUP_COST if len(self.events) > 1 else float("inf")
        for cost, shape, places in self.beam:
            if shape > limit:
                continue
            fing = tuple(sorted((s, f) for s, f, _ in places.values()))
            left = tuple(sorted((s, f) for s, f, tap in places.values() if not tap))
            taps = frozenset((s, f) for s, f, tap in places.values() if tap)
            out.append(Cand(fing, left, taps, cost, shape))
        return out[:MAX_CANDIDATES]


def build_units(events: list[Event], opens: list[int], max_fret: int, max_span: int,
                tpb: int, tapping: bool, tap_steps: set[int] | None = None
                ) -> tuple[list[list[Event]], list[list[Cand]], int]:
    """イベントを「運指を一緒に決めるまとまり」に分け、それぞれの候補を作る。

    前の音がまだ鳴っているうちに始まる音は同じまとまりに入れる(let ring のアルペジオや、
    コードを押さえたままのメロディ)。戻り値: (まとまり, 候補, 省いた音の数)
    """
    units: list[list[Event]] = []
    cand_lists: list[list[Cand]] = []
    dropped = 0
    builder = GroupBuilder(opens, max_fret, max_span, tpb, tapping, tap_steps)
    active = False

    def flush():
        if active:
            units.append(list(builder.events))
            cand_lists.append(builder.candidates())

    for ev in events:
        ringing = active and max(e.end_tick for e in builder.events) > ev.on_tick + tpb / 4
        if ringing and builder.add(ev):
            continue
        flush()
        active = builder.start(ev)
        if not active:  # 単独でも押さえられない和音: 音を省いて候補を作る
            shapes, lost = playable_candidates(ev.pitches, opens, max_fret, max_span)
            dropped += lost
            if shapes:
                units.append([ev])
                cand_lists.append(plain_cands(shapes))
    flush()
    return units, cand_lists, dropped


def assign_fingerings(events: dict[int, Event], opens: list[int], grid: int,
                      max_fret: int, max_span: int, tpb: int = 480,
                      tapping: bool = False, tap_steps: set[int] | None = None) -> Assignment:
    """曲全体で左手の移動が最小になるよう、各和音の運指を選ぶ(ビタビ)。

    tapping=True のとき、「左手を動かさず右手でタッピングする」選択肢も候補に入れる:
    レガートでつながる単音と、鳴らしっぱなしのコードの上に乗る高い音が対象。
    tap_steps を渡すと、そのステップの音だけをタッピングの対象にする。
    """
    if not events:
        return Assignment({}, set(), 0)
    units, cand_lists, dropped_total = build_units(list(events.values()), opens, max_fret,
                                                   max_span, tpb, tapping, tap_steps)
    if not units:
        return Assignment({}, set(), dropped_total)

    def single(i: int) -> bool:
        return len(units[i]) == 1 and len(cand_lists[i][0].fing) == 1

    def legato_into(i: int) -> bool:
        return (0 < i < len(units) and single(i - 1) and single(i)
                and is_legato(units[i - 1][0], units[i][0], tpb))

    signatures = [[shape_signature(c) for c in cands] for cands in cand_lists]
    # 状態 = (累積コスト, 直前の状態index, 候補index, 左手ポジション, タッピングか)
    # ポジションは「まだ決まっていない」場合 None (開放弦だけで始まる曲など)
    layers: list[list[tuple[float, int, int, int | None, bool]]] = []
    for i, cands in enumerate(cand_lists):
        prev_shapes: dict = {}  # フォームの形 → ポジションごとの最良の直前状態
        if i == 0:
            prev_best = {None: (0.0, -1)}
            move_weight = 0.0
        else:
            prev_best = {}  # ポジションごとの最良の直前状態
            for k, (cost, _, pci, pos, _) in enumerate(layers[-1]):
                if pos not in prev_best or cost < prev_best[pos][0]:
                    prev_best[pos] = (cost, k)
                sig = signatures[i - 1][pci]
                if sig is not None and pos is not None:
                    same = prev_shapes.setdefault(sig, {})
                    if pos not in same or cost < same[pos][0]:
                        same[pos] = (cost, k)
            gap_beats = (units[i][0].step - units[i - 1][-1].step) * 4 / grid
            move_weight = 1.0 / (1.0 + gap_beats / 2)  # 間が空くほど移動は楽
        can_tap = (tapping and (legato_into(i) or legato_into(i + 1))
                   and (tap_steps is None or units[i][0].step in tap_steps))
        layer = []
        for ci, cand in enumerate(cands):
            sc = cand.cost
            rng = fret_range(cand.left)
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
                # 直前と同じ形のまま平行移動するのは楽(オクターブ奏法やパワーコードの移動)
                for ppos, (pcost, k) in prev_shapes.get(signatures[i][ci], {}).items():
                    total = pcost + SAME_SHAPE_MOVE * abs(pos - ppos) * move_weight
                    if total < best[0]:
                        best = (total, k)
                layer.append((best[0] + sc, best[1], ci, pos, False))
            if can_tap and cand.left[0][0] >= len(opens) - TAP_STRINGS:
                fret = rng[0]  # 左手より上のフレットを右手で叩く。左手は動かない
                layer.extend((pcost + TAP_COST + 0.1 * fret, k, ci, ppos, True)
                             for ppos, (pcost, k) in prev_best.items()
                             if ppos is not None and fret > ppos + HAND_REACH)
        layers.append(layer)

    j = min(range(len(layers[-1])), key=lambda k: layers[-1][k][0])
    result: dict[int, Fingering] = {}
    tapped = set()
    for i in range(len(units) - 1, -1, -1):
        _, prev_j, ci, _, tap = layers[i][j]
        cand = cand_lists[i][ci]
        for ev in units[i]:  # まとまりの運指から、各イベントで弾く弦だけ取り出す
            result[ev.step] = tuple((st, f) for st, f in cand.fing if opens[st] + f in ev.ends)
            if any(note in cand.taps for note in result[ev.step]):
                tapped.add(ev.step)
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


# ---------------------------------------------------------------- 難易度

LEVEL_THRESHOLDS = (1.5, 3.0, 4.5, 6.5)  # 点数がこれ以上になるごとに Lv が1つ上がる (Lv1〜5)


def difficulty_level(points: float) -> int:
    return 1 + sum(points >= t for t in LEVEL_THRESHOLDS)


def measure_difficulty(events: dict[int, Event], fingerings: dict[int, Fingering],
                       tapped: set[int], dead: set[int], measures: list[Measure],
                       grid: int, tpb: int, bpm: float | None,
                       opens: list[int]) -> dict[int, float]:
    """小節ごとの弾きにくさの点数 (小節index → 点数)。difficulty_level で段階にする。

    見ているもの:
      速さ      1秒あたりの発音数(テンポを反映)
      ストレッチ 同時に押さえているフレットの幅
      移動      左手のポジション移動の距離と、その間の時間
      弦飛び    速い単音フレーズでの2本以上離れた弦への移動
      コード    押さえる指の本数、同時に弾く音の間で飛ばす弦の本数、
                低音弦だけ高いフレットになる逆向きの形
      ハイフレット 15フレットより上
      タッピング 右手で叩く音の数、低音弦、右手の跳躍
    """
    sec = 60 / (bpm or 120) * 4 / grid   # 1ステップの秒数
    steps = sorted(fingerings)
    out: dict[int, float] = {}
    pos: int | None = None               # 左手の人差し指の位置
    prev_step: int | None = None
    prev_single: tuple[int, int] | None = None   # 直前の単音 (弦, ステップ)
    prev_tap: int | None = None
    prev_form = None                     # 直前に押さえたフォームの形
    i = 0
    for mi, (ms, ml, _, _) in enumerate(measures):
        in_bar = []
        while i < len(steps) and steps[i] < ms + ml:
            if steps[i] >= ms:
                in_bar.append(i)
            i += 1
        if not in_bar:
            continue
        rate = len(in_bar) / (ml * sec)
        speed = 0.0 if rate < 3 else 0.5 if rate < 5 else 1.5 if rate < 8 else 2.5 if rate < 11 else 4.0
        stretch = shift = skips = taps = high = chord = 0.0
        for k in in_bar:
            step = steps[k]
            fing = fingerings[step]
            if step in dead:  # 空ピッキングは左手を浮かせるだけ
                prev_single = None
                continue
            if step in tapped:
                taps += 0.4 + 0.25 * sum(s <= 2 for s, _ in fing)  # 3弦より低い弦は叩きにくい
                fret = max(f for _, f in fing)
                if prev_tap is not None and abs(fret - prev_tap) > 5:
                    taps += 0.4
                prev_tap = fret
                prev_single = None
                continue
            own = [f for _, f in fing if f > 0]
            held = list(own)   # いま押さえたままになっている音も含めたフレット
            reused = {s for s, _ in fing}   # あとから別の音を弾いた弦(前の音はもう鳴っていない)
            for j in range(k - 1, max(0, k - 8) - 1, -1):
                p = steps[j]
                if p not in tapped and p not in dead \
                        and events[p].end_tick > events[step].on_tick + tpb / 8:
                    held += [f for s, f in fingerings[p] if f > 0 and s not in reused]
                reused |= {s for s, _ in fingerings[p]}
            if held and max(held) - min(held) > 4 and own:
                held = own  # 押さえたままでは届かない: 前の音は離して弾くものとみなす
            if held:
                span = max(held) - min(held)
                stretch = max(stretch, 0.0 if span <= 2 else 1.0 if span == 3
                              else 2.5 if span == 4 else 4.0)
            if own:
                lo, hi = min(own), max(own)
                new = pos
                if pos is None or lo < pos:
                    new = lo
                elif hi > pos + HAND_REACH:
                    new = min(lo, hi - HAND_REACH)
                form = (tuple(sorted((s, f - lo) for s, f in fing if f > 0))
                        if len(own) >= 2 else None)
                if pos is not None and new != pos and prev_step is not None:
                    gap = max(0, step - prev_step) * sec
                    move = min(2.0, abs(new - pos) / (4 * (gap + 0.15)))
                    # 同じ形のままの平行移動(オクターブ奏法など)は楽
                    shift += move * (SAME_SHAPE_MOVE if form and form == prev_form else 1.0)
                pos = new
                prev_form = form
                if hi > 15:
                    high = 0.3
                fingers = len(own) - max(0, own.count(lo) - 1)
                chord = max(chord, 0.7 if fingers >= 4 else 0.3 if fingers == 3 else 0.0)
            if len(fing) > 1:  # 低音弦だけ高いフレットになる形
                chord = max(chord, min(2.0, 0.7 * max(0, inversion(fing) - 2)))
            if len(fing) > 1:  # 同時に弾く音の間で、必要以上に弦を飛ばす形
                chord = max(chord, min(1.2, 0.6 * skip_cost((s, opens[s] + f) for s, f in fing)))
            if len(fing) == 1:
                if prev_single and abs(fing[0][0] - prev_single[0]) >= 2 \
                        and (step - prev_single[1]) * sec < 0.25:
                    skips += 0.3
                prev_single = (fing[0][0], step)
            else:
                prev_single = None
            prev_step = step
        out[mi] = round(speed + stretch + min(shift, 3.0) + min(skips, 1.5)
                        + min(taps, 3.0) + high + chord, 2)
    return out


def choose_tap_steps(events: dict[int, Event], measures: list[Measure], opens: list[int],
                     grid: int, max_fret: int, max_span: int, tpb: int,
                     bpm: float | None) -> tuple[Assignment, set[int]]:
    """タッピングを使うと難易度が下がる小節を探し、そこだけタッピングを許して運指を決める。

    戻り値: (運指, タッピングを採用した小節index)
    """
    plain = assign_fingerings(events, opens, grid, max_fret, max_span, tpb)
    tap = assign_fingerings(events, opens, grid, max_fret, max_span, tpb, tapping=True)
    args = (set(), measures, grid, tpb, bpm, opens)
    d_plain = measure_difficulty(events, plain.fingerings, plain.tapped, *args)
    d_tap = measure_difficulty(events, tap.fingerings, tap.tapped, *args)
    tapped_bars = {next(i for i, m in enumerate(measures) if m[0] <= s < m[0] + m[1])
                   for s in tap.tapped}
    easier = {mi for mi in tapped_bars
              if difficulty_level(d_tap.get(mi, 0)) < difficulty_level(d_plain.get(mi, 0))}
    if not easier:
        return plain, set()
    allowed = {s for s in events
               if any(measures[mi][0] <= s < measures[mi][0] + measures[mi][1] for mi in easier)}
    return assign_fingerings(events, opens, grid, max_fret, max_span, tpb,
                             tapping=True, tap_steps=allowed), easier


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
    difficulty: dict[int, float] = field(default_factory=dict) # 小節index → 弾きにくさの点数
    tempos: list[tuple[int, int]] = field(default_factory=list)  # 再生用: (tick, 1拍のマイクロ秒)

    def tick(self, step: int) -> int:
        """ステップ番号 → tick。編集内容のキーに使う(グリッドを変えても同じ位置を指す)。"""
        return round(step * self.tpb * 4 / self.grid)


def analyze(mid: mido.MidiFile, *, tuning: str = "standard", capo: int = 0,
            grid: int | None = None, tracks: list[int] | None = None,
            channels: list[int] | None = None, transpose: int = 0,
            max_fret: int = 20, max_span: int = 4,
            legato: bool = False, tapping: bool | str = False,
            chords: bool = True, expression: bool = True,
            dead_notes: list[int] | None = None,
            edits: dict | None = None) -> Score:
    warnings: list[str] = []
    opens = [p + capo for p in parse_tuning(tuning)]
    max_fret -= capo
    top = max(opens) + max_fret
    song = load_song(mid, tracks, channels, transpose, frozenset(dead_notes or ()))
    if dead_notes is None:
        # 指定が無ければ、ギターで弾けない高さの音を空ピッキング用のキーとみなして X で出す
        high = collections.Counter(n.pitch for n in song.notes if n.pitch > top)
        if high:
            song = load_song(mid, tracks, channels, transpose, frozenset(high))
            names = ", ".join(f"{pitch_class_name(p)}{p // 12 - 1}(ノート番号{p})×{c}"
                              for p, c in sorted(high.items()))
            warnings.append(f"音域より高い {sum(high.values())} 音を X (空ピッキング)として表示しました: "
                            f"{names}。不要なら --dead-notes none")
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

    # dead_notes に入らなかった音域より高い音は弾けないので無視する。
    # 低すぎる音はオクターブ上げて収める
    too_high = collections.Counter(n.pitch for n in song.notes if n.pitch > top)
    song.notes = [n for n in song.notes if n.pitch <= top]
    if too_high:
        names = ", ".join(f"{pitch_class_name(p)}{p // 12 - 1}(ノート番号{p})×{c}"
                          for p, c in sorted(too_high.items()))
        warnings.append(f"音域より高い {sum(too_high.values())} 音を無視しました: {names}")
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
    step_ticks = tpb * 4 / grid
    last = max(max(events), round(max(e.end_tick for e in events.values()) / step_ticks) - 1)
    measures = build_measures(song, grid, last)
    if tapping == "auto":  # 難易度が下がる小節だけタッピングにする
        asg, eased = choose_tap_steps(events, measures, opens, grid, max_fret, max_span,
                                      tpb, song.bpm)
        if eased:
            bars = ", ".join(str(mi + 1) for mi in sorted(eased))
            warnings.append(f"タッピングのほうが易しい {len(eased)} 小節にタッピングを採用しました: {bars}")
    else:
        asg = assign_fingerings(events, opens, grid, max_fret, max_span, tpb, bool(tapping))
    if asg.dropped:
        warnings.append(f"同時に押さえられない {asg.dropped} 音を省略しました")
    if not asg.fingerings:
        raise ValueError("このチューニングで弾ける音がありません")

    dead_steps = add_dead_strums(song, events, asg, opens, step_ticks)
    if edits:
        apply_note_edits(asg, opens, max_fret, edits, step_ticks)
        if not asg.fingerings:
            raise ValueError("すべての音が非表示になっています")
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
                 find_sections(song, measures, step_ticks),
                 measure_difficulty(events, asg.fingerings, asg.tapped, dead_steps, measures,
                                    grid, tpb, song.bpm, opens),
                 song.tempos)


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
    """弦の変更・音の非表示を運指に反映する。成立しない指定は無視する。

    同じ和音への指定はまとめて同時に適用する(「AをどかしてからBをそこへ」が、
    指定を記録した順番に関係なく通るように)。
    """
    by_step: dict[int, dict[int, dict]] = {}
    for key, edit in edits.get("notes", {}).items():
        try:
            tick, pitch = (int(x) for x in key.split(":"))
        except ValueError:
            continue
        if isinstance(edit, dict):
            by_step.setdefault(round(tick / step_ticks), {})[pitch] = edit
    for step, wanted in by_step.items():
        fing = asg.fingerings.get(step)
        if not fing:
            continue
        original = {opens[s] + f: (s, f) for s, f in fing}
        placed = dict(original)
        moved = []
        for pitch, edit in wanted.items():
            if pitch not in placed:
                continue
            if edit.get("hide"):
                del placed[pitch]
                continue
            s = edit.get("string")
            if isinstance(s, int) and 0 <= s < len(opens) and 0 <= pitch - opens[s] <= max_fret:
                placed[pitch] = (s, pitch - opens[s])
                moved.append(pitch)
        # 同じ弦に2音が重なったら、動かした音を1つずつ元に戻す
        while True:
            used = collections.Counter(s for s, _ in placed.values())
            clash = [p for p in reversed(moved) if used[placed[p][0]] > 1]
            if not clash:
                break
            # 動かしていない音とぶつかっている指定を先に取り消す
            fixed = {placed[p][0] for p in placed if p not in moved}
            pitch = next((p for p in clash if placed[p][0] in fixed), clash[0])
            placed[pitch] = original[pitch]
            moved.remove(pitch)
        if placed:
            asg.fingerings[step] = tuple(sorted(placed.values()))
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


def dead_note_list(text: str) -> list[int]:
    return [] if text.strip().lower() in ("none", "") else int_list(text)


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
                    help="タッピングを積極的に使う (--legato を含む)")
    ap.add_argument("--tapping-auto", action="store_true",
                    help="タッピングにしたほうが難易度の段階が下がる小節だけ、タッピングを使う")
    ap.add_argument("--dead-notes", type=dead_note_list,
                    help="空ピッキング(ブラッシング)を表すノート番号(カンマ区切り)。X として表示する。"
                         "既定はギターの音域より高い音すべて。none で無視")
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
    if args.tapping_auto:
        options["tapping"] = "auto"
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
