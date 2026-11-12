"""奏法(弾き方の種類)の判定。

音の並び方から、区間ごとに「オクターブ奏法」「スウィープ」「アルペジオ」などを推定する。
運指は奏法ごとに弾きやすい形が違うので、この判定を運指の決め方の切り替えに使う。
"""
from __future__ import annotations

# コード → 譜面に出す名前
LABELS = {
    "octave": "オクターブ",
    "power": "パワーコード",
    "strum": "コードストローク",
    "chord": "コード",
    "double": "ダブルストップ",
    "arpeggio": "アルペジオ",
    "sweep": "スウィープ",
    "run": "速弾き",
    "melody": "単音メロディ",
    "tapping": "タッピング",
    "mixed": "アルペジオ＋メロディ",
}

# 3音以上続いていれば、周りと混ぜずにその奏法として残すもの
DISTINCT = {"octave", "power", "sweep", "run", "tapping", "strum"}
MAJORITY = 0.6       # 小節の中でこの割合を超える奏法があれば、その小節はその奏法とみなす

FAST = 0.22          # これより短い間隔(秒)で続く単音を「速い」とみなす
MIN_SWEEP = 5        # スウィープとみなす最小の音数
MIN_RUN = 5          # 速弾きとみなす最小の音数
MIN_ARPEGGIO = 3     # アルペジオとみなす最小の音数


def chord_kind(pitches: list[int]) -> str:
    """同時に鳴る音の組み合わせの種類。"""
    if len(pitches) == 1:
        return "single"
    intervals = {(p - pitches[0]) % 12 for p in pitches}
    if intervals == {0}:
        return "octave"
    if intervals <= {0, 7} and len(pitches) <= 4:
        return "power"
    return "double" if len(pitches) == 2 else "chord"


def classify(events, steps: list[int], dead: set[int], ringing: set[int], tapped: set[int],
             seconds_per_step: float, steps_per_beat: float) -> dict[int, str]:
    """各イベント(ステップ)の奏法。

    events: ステップ → Event、steps: 譜面に出るステップ(昇順)、dead: 空ピッキング、
    ringing: 次の音が鳴ったあとも伸びる音、tapped: 右手で叩く音
    """
    labels: dict[int, str] = {}
    kinds = {s: "dead" if s in dead else "tapping" if s in tapped
             else chord_kind(events[s].pitches) for s in steps}

    # --- 和音: 近くに空ピッキングや別の和音があれば、かき鳴らし(ストローク)
    for i, s in enumerate(steps):
        kind = kinds[s]
        if kind in ("octave", "power", "double", "tapping"):
            labels[s] = kind
        elif kind == "chord":
            near = [t for t in steps[max(0, i - 4):i + 5]
                    if t != s and abs(t - s) <= steps_per_beat and kinds[t] in ("chord", "dead")]
            labels[s] = "strum" if near else "chord"

    # --- 単音の連なりを切り出して、種類を決める
    i = 0
    while i < len(steps):
        if kinds[steps[i]] != "single":
            i += 1
            continue
        j = i
        while (j + 1 < len(steps) and kinds[steps[j + 1]] == "single"
               and steps[j + 1] - steps[j] <= steps_per_beat):
            j += 1
        _label_singles(events, steps[i:j + 1], ringing, seconds_per_step, labels)
        i = j + 1

    # --- 空ピッキングは、前後どちらかの奏法に含める
    for i, s in enumerate(steps):
        if kinds[s] == "dead":
            around = [labels.get(t) for t in (steps[i - 1] if i else None,
                                              steps[i + 1] if i + 1 < len(steps) else None)
                      if t is not None]
            labels[s] = next((x for x in around if x in ("strum", "chord", "power", "octave")),
                             "strum")
            if labels[s] == "chord":
                labels[s] = "strum"
    return labels


def _label_singles(events, run: list[int], ringing: set[int], sec: float,
                   labels: dict[int, str]) -> None:
    pitch = [events[s].pitches[0] for s in run]
    n = len(run)
    done = [False] * n

    def mark(a: int, b: int, label: str):
        for k in range(a, b + 1):
            labels[run[k]] = label
            done[k] = True

    # アルペジオ: 鳴らしっぱなしの音が続く(最後の音まで含める)
    k = 0
    while k < n:
        if run[k] in ringing:
            e = k
            while e + 1 < n and run[e + 1] in ringing:
                e += 1
            end = min(n - 1, e + 1)
            if end - k + 1 >= MIN_ARPEGGIO:
                mark(k, end, "arpeggio")
            k = end + 1
        else:
            k += 1

    # 速い単音の連なり: 3度〜5度で一方向に進めばスウィープ、2度中心なら速弾き
    k = 0
    while k < n:
        if done[k]:
            k += 1
            continue
        e = k
        while (e + 1 < n and not done[e + 1] and (run[e + 1] - run[e]) * sec <= FAST):
            e += 1
        if e > k:
            _label_fast(pitch, k, e, mark)
        k = e + 1
    for k in range(n):
        if not done[k]:
            labels[run[k]] = "melody"


def _label_fast(pitch: list[int], a: int, b: int, mark) -> None:
    """速い単音の区間 [a, b] の中から、スウィープと速弾きを見つける。"""
    k = a
    while k < b:
        # k から、コードトーンを一方向に(折り返しは1回まで)進む区間
        e, direction, turns = k, 0, 0
        while e < b:
            step = pitch[e + 1] - pitch[e]
            if not 3 <= abs(step) <= 7:
                break
            d = 1 if step > 0 else -1
            if direction and d != direction:
                turns += 1
                if turns > 1:
                    break
            direction = d
            e += 1
        if e - k + 1 >= MIN_SWEEP:
            mark(k, e, "sweep")
            k = e + 1
            continue
        # k から、2度(半音・全音)中心で進む区間
        e, small = k, 0
        while e < b and abs(pitch[e + 1] - pitch[e]) <= 4:
            small += abs(pitch[e + 1] - pitch[e]) <= 2
            e += 1
        if e - k + 1 >= MIN_RUN and small >= 0.75 * (e - k):
            mark(k, e, "run")
            k = e + 1
        else:
            k += 1


def smooth(labels: dict[int, str], measures) -> dict[int, str]:
    """細かく切り替わる判定を小節単位でまとめる。

    はっきりした奏法(オクターブ、スウィープなど)が3音以上続く部分はそのまま残し、
    残りは小節ごとに多数派の奏法にそろえる。多数派が無ければ「アルペジオ＋メロディ」。
    measures: (開始ステップ, 長さ, ...) のリスト
    """
    steps = sorted(labels)
    keep: set[int] = set()
    i = 0
    while i < len(steps):
        j = i
        while j + 1 < len(steps) and labels[steps[j + 1]] == labels[steps[i]]:
            j += 1
        if labels[steps[i]] in DISTINCT and j - i + 1 >= 3:
            keep.update(steps[i:j + 1])
        i = j + 1
    out = dict(labels)
    for start, length, *_ in measures:
        rest = [s for s in steps if start <= s < start + length and s not in keep]
        if not rest:
            continue
        counts: dict[str, int] = {}
        for s in rest:
            counts[labels[s]] = counts.get(labels[s], 0) + 1
        top = max(counts, key=counts.get)
        label = top if counts[top] >= MAJORITY * len(rest) else "mixed"
        for s in rest:
            out[s] = label
    return out


def passage_starts(labels: dict[int, str]) -> dict[int, str]:
    """奏法が切り替わる位置だけを取り出す(ステップ → 奏法)。"""
    out: dict[int, str] = {}
    last = None
    for step in sorted(labels):
        if labels[step] != last:
            out[step] = labels[step]
        last = labels[step]
    return out
