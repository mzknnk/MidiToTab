import os
import sys

import mido

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import midi2tab as m


def make_midi(events, tpb=480, sig=(4, 4)):
    """events: [(拍位置, [音高...], 長さ(拍))]"""
    mid = mido.MidiFile(ticks_per_beat=tpb)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("time_signature", numerator=sig[0], denominator=sig[1], time=0))
    msgs = []
    for beat, pitches, dur in events:
        for p in pitches:
            msgs.append((int(beat * tpb), 1, p))
            msgs.append((int((beat + dur) * tpb), 0, p))
    msgs.sort()
    now = 0
    for tick, on, p in msgs:
        track.append(mido.Message("note_on" if on else "note_off", note=p,
                                  velocity=80 if on else 0, time=tick - now))
        now = tick
    return mid


def fingerings_for(events, **kw):
    mid = make_midi(events)
    song = m.load_song(mid)
    grid = m.choose_grid(song)
    events = m.quantize(song, grid)
    opens = m.parse_tuning(kw.get("tuning", "standard"))
    asg = m.assign_fingerings(events, opens, grid, 20, 4, tapping=kw.get("tapping", False))
    return (asg.fingerings, asg.dropped), grid


def test_parse_note_and_tuning():
    assert m.parse_note("C4") == 60
    assert m.parse_note("Bb1") == 34
    assert m.parse_tuning("standard") == [40, 45, 50, 55, 59, 64]
    assert m.parse_tuning("D2 A2 D3 G3 B3 E4")[0] == 38


def test_open_strings_preferred():
    (fings, dropped), _ = fingerings_for([(i, [p], 1) for i, p in enumerate([40, 45, 50, 55, 59, 64])])
    assert dropped == 0
    assert [f for fing in fings.values() for _, f in fing] == [0] * 6


def test_pitches_are_preserved():
    events = [(i * 0.5, [p], 0.5) for i, p in enumerate([60, 62, 64, 65, 67, 69, 71, 72])]
    (fings, _), _ = fingerings_for(events)
    opens = m.parse_tuning("standard")
    got = [opens[s] + f for step in sorted(fings) for s, f in fings[step]]
    assert got == [60, 62, 64, 65, 67, 69, 71, 72]


def test_open_c_chord():
    (fings, dropped), _ = fingerings_for([(0, [48, 52, 55, 60, 64], 4)])
    assert dropped == 0
    assert sorted(fings[0]) == [(1, 3), (2, 2), (3, 0), (4, 1), (5, 0)]


def test_high_melody_stays_in_position():
    # 12フレット付近のフレーズ: 開放弦へ飛ばずポジションを保つ
    events = [(i * 0.5, [p], 0.5) for i, p in enumerate([76, 79, 81, 79, 76, 79, 81, 84])]
    (fings, _), _ = fingerings_for(events)
    frets = [f for step in sorted(fings) for _, f in fings[step]]
    assert max(frets) - min(frets) <= 5


def test_auto_grid():
    assert fingerings_for([(i, [60], 1) for i in range(4)])[1] == 4
    assert fingerings_for([(i * 0.25, [60], 0.25) for i in range(8)])[1] == 16
    assert fingerings_for([(i / 3, [60], 0.3) for i in range(6)])[1] == 12


def test_unplayable_notes_dropped_and_range_fitted():
    cands, dropped = m.playable_candidates([40, 41, 42], m.parse_tuning("standard"), 20, 4)
    assert dropped >= 1 and cands
    assert m.fit_range(28, m.parse_tuning("standard"), 20) == 40


def test_render_layout():
    mid = make_midi([(0, [40], 1), (1, [45], 1), (4, [64], 1)])
    text = m.transcribe(mid, grid=4)
    lines = text.splitlines()
    tab = [l for l in lines if "|" in l]
    assert len(tab) == 6
    assert tab[0].startswith("E|") and tab[-1].startswith("E|")
    assert len({len(l) for l in tab}) == 1
    assert tab[-1] == "E|-0-------|---------|"   # 6弦: 1拍目に開放
    assert tab[-2] == "A|---0-----|---------|"   # 5弦: 2拍目に開放
    assert tab[0] == "E|---------|-0-------|"    # 1弦: 2小節目の頭


def test_drums_excluded_and_cli(tmp_path, capsys):
    mid = make_midi([(0, [60], 1)])
    mid.tracks[0].insert(1, mido.Message("note_on", channel=9, note=36, velocity=90, time=0))
    path = tmp_path / "a.mid"
    mid.save(path)
    assert m.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "Tuning: E A D G B E" in out
    assert out.count("1") >= 1 and "省略" not in out
    assert m.main([str(path), "--list"]) == 0


def rhythm_of(events, **kw):
    score = m.analyze(make_midi(events), **kw)
    return score, [[(it.length, it.dur.k, it.dur.dotted, it.dur.triplet, it.step is None, it.tied)
                    for it in bar] for bar in score.rhythm]


def test_rhythm_values():
    # 4分, 8分x2, 付点4分, 8分 | 全音符
    _, bars = rhythm_of([(0, [60], 1), (1, [62], .5), (1.5, [64], .5), (2, [65], 1.5),
                         (3.5, [67], .5), (4, [60], 4)], grid=8)
    assert [b[:3] for b in bars[0]] == [(2, 2, False), (1, 3, False), (1, 3, False),
                                        (3, 2, True), (1, 3, False)]
    assert bars[1] == [(8, 0, False, False, False, False)]


def test_rests_and_ties():
    # 短く切った音のあとは休符、小節をまたぐ音はタイ
    _, bars = rhythm_of([(0, [60], 1), (2, [62], 1), (3, [64], 3)], grid=4)
    assert [(b[0], b[4], b[5]) for b in bars[0]] == [(1, False, False), (1, True, False),
                                                     (1, False, False), (1, False, False)]
    assert bars[1][0][5] is True and bars[1][0][0] == 2      # タイで2拍
    assert bars[1][1][4] is True                             # 残りは休符


def test_triplets():
    _, bars = rhythm_of([(i / 3, [60 + i], 1 / 3) for i in range(6)] + [(2, [60], 2)])
    assert all(b[3] and b[1] == 3 for b in bars[0][:6])
    assert bars[0][6][:2] == (6, 1)


def test_let_ring_detected():
    score, _ = rhythm_of([(0, [45], 4), (1, [57], 3), (2, [60], 2), (3, [64], 1)])
    assert score.ringing == {0, 1, 2}  # 最後の音以外は次の発音後も鳴り続ける


def test_tapping_and_legato():
    # 1本弦で 5-8-17 を切れ目なく繰り返す: 17フレットは左手が届かないのでタッピング
    lick = [(i * 0.25, [[69, 72, 81][i % 3]], 0.25) for i in range(9)]
    score = m.analyze(make_midi(lick), tuning="E4", tapping=True)
    frets = [score.fingerings[s][0][1] for s in sorted(score.fingerings)]
    assert frets == [5, 8, 17] * 3
    assert score.tapped == {2, 5, 8}
    assert [score.techs[s][0] for s in sorted(score.techs)] == ["H", "T", "P", "H", "T", "P", "H", "T"]
    # 指定しなければ奏法記号は付けない
    assert m.analyze(make_midi(lick), tuning="E4").techs == {}


def test_html_output(tmp_path):
    mid = make_midi([(0, [48, 52, 55], 1), (1, [60], .5), (1.5, [62], .25), (1.75, [64], .25),
                     (2, [65], 1), (4, [60], 2)])
    path, out = tmp_path / "a.mid", tmp_path / "a.html"
    mid.save(path)
    assert m.main([str(path), "-o", str(out), "--title", "テスト<曲>"]) == 0
    html = out.read_text(encoding="utf-8")
    assert "テスト&lt;曲&gt;" in html and html.count("<svg") >= 1
    import xml.dom.minidom
    for svg in html.split("<svg")[1:]:
        xml.dom.minidom.parseString("<svg" + svg.split("</svg>")[0] + "</svg>")


def test_chord_names():
    import chords
    pc = lambda *names: {m.parse_note(n + "4") % 12 for n in names}
    assert chords.name_chord(pc("A", "C", "E"), 9) == "Am"
    assert chords.name_chord(pc("E", "G#", "B"), 8) == "E/G#"
    assert chords.name_chord(pc("C", "E", "G"), 7) == "C/G"
    assert chords.name_chord(pc("F", "A", "C", "E"), 5) == "Fmaj7"
    assert chords.name_chord(pc("A", "C", "E", "G"), 9) == "Am7"
    assert chords.name_chord(pc("A", "C", "E", "G"), 0) == "C6"
    assert chords.name_chord(pc("Bb", "D", "F"), 2) == "Bb/D"
    assert chords.name_chord(pc("C", "D", "E"), 0) is None


def test_chords_detected_from_arpeggio():
    def arp(start, notes):
        return [(start + i * 0.5, [p], 2 - i * 0.5) for i, p in enumerate(notes)]
    events = (arp(0, [57, 60, 64, 69]) + arp(2, [56, 59, 64, 71]) + arp(4, [55, 60, 64, 72])
              + arp(6, [54, 57, 62, 66]) + arp(8, [53, 60, 57, 64]) + arp(10, [53, 60, 57, 64])
              + [(12 + i * 0.25, [p], 0.25) for i, p in enumerate([60, 62, 64, 65] * 4)])
    score = m.analyze(make_midi(events))
    per_beat = score.grid // 4
    assert {s // per_beat: n for s, n in score.chords.items()} == {
        0: "Am", 2: "E/G#", 4: "C/G", 6: "D/F#", 8: "Fmaj7"}  # スケールにはコードを付けない
    assert m.analyze(make_midi(events), chords=False).chords == {}


LICK = [(0, [64], 1), (1, [67], 1), (2, [48, 52, 55], 2)]  # 1弦開放, 1弦3f, Cコード


def edited(edits, **options):
    edits = dict(edits, options=options)
    return m.analyze_with_edits(make_midi(LICK), {}, edits)


def test_edit_move_string_and_hide():
    base = edited({})
    assert base.fingerings[1] == ((5, 3),)                      # G4 = 1弦3フレット
    key = f"{base.tick(1)}:67"
    moved = edited({"notes": {key: {"string": 4}}})
    assert moved.fingerings[1] == ((4, 8),)                     # 2弦8フレットへ
    assert edited({"notes": {key: {"string": 0}}}).fingerings[1] == ((5, 3),)  # 弾けない弦は無視
    hidden = edited({"notes": {key: {"hide": True}}})
    assert 1 not in hidden.fingerings and len(hidden.fingerings) == 2
    # グリッドを変えても同じ音を指す
    assert edited({"notes": {key: {"string": 4}}}, grid=16).fingerings[4] == ((4, 8),)


def test_edit_techs_and_chords():
    base = edited({})
    assert base.techs == {} and base.chords == {0: "C"}  # E, G もCの構成音なので頭から
    t0, t1, t2 = (str(base.tick(i)) for i in range(3))
    score = edited({"techs": {t1: "H"}, "chords": {t2: "Cadd9", t1: "G"}})
    assert score.techs == {1: ("H", 0)}
    assert score.chords == {0: "C", 1: "G", 2: "Cadd9"}
    assert edited({"chords": {t0: ""}}).chords == {}
    # 自動判定の H を手動で取り消す
    legato = [(0, [64], 1), (1, [67], 1)]
    auto = m.analyze_with_edits(make_midi(legato), {}, {"options": {"legato": True}})
    assert auto.techs == {1: ("H", 0)}
    off = m.analyze_with_edits(make_midi(legato), {}, {"options": {"legato": True},
                                                       "techs": {str(auto.tick(1)): ""}})
    assert off.techs == {}


def test_editor_server(tmp_path):
    import json
    import threading
    import urllib.error
    import urllib.request
    import editor

    path = tmp_path / "song.mid.edits.json"
    session = editor.Session(make_midi(LICK), {}, {}, str(path), "song")
    server = editor.make_server(session, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(url, body=None, headers=None):
        req = urllib.request.Request(base + url, data=body, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    try:
        assert b"midi2tab" in call("/")[1]
        state = json.loads(call("/api/state")[1])
        assert state["title"] == "song" and len(state["svgs"]) == 1 and 'class="n"' in state["svgs"][0]
        new = {"title": "新しい曲名", "options": {"capo": 2}, "chords": {"960": "C/E"}}
        status, body = call("/api/edits", json.dumps(new).encode())
        assert status == 200
        out = json.loads(body)
        assert out["title"] == "新しい曲名" and "Capo 2" in out["meta"] and "C/E" in out["svgs"][0]
        assert json.loads(path.read_text(encoding="utf-8")) == new          # 自動保存
        # 不正な設定は拒否され、状態は変わらない
        status, body = call("/api/edits", json.dumps({"options": {"tuning": "xyz"}}).encode())
        assert status == 400 and "error" in json.loads(body)
        assert session.edits == new
        # 他サイトからの書き込みは拒否
        assert call("/api/edits", b"{}", {"Origin": "http://evil.example"})[0] == 403
    finally:
        server.shutdown()
        server.server_close()


def test_cli_uses_saved_edits(tmp_path, capsys):
    import json
    mid_path = tmp_path / "s.mid"
    make_midi(LICK).save(mid_path)
    (tmp_path / "s.mid.edits.json").write_text(json.dumps(
        {"title": "保存した題", "options": {"capo": 3}, "chords": {"960": "Xm"}}), encoding="utf-8")
    out = tmp_path / "s.html"
    assert m.main([str(mid_path), "-o", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    assert "保存した題" in html and "Capo 3" in html and ">Xm<" in html
    assert m.main([str(mid_path), "-o", str(out), "--capo", "0"]) == 0   # 明示した指定が優先
    assert "Capo" not in out.read_text(encoding="utf-8")


def test_pdf_output(tmp_path):
    import pytest
    import pdfout
    if not pdfout.find_browser():
        pytest.skip("Chrome系ブラウザが無い")
    mid_path, out = tmp_path / "s.mid", tmp_path / "s.pdf"
    make_midi(LICK).save(mid_path)
    assert m.main([str(mid_path), "-o", str(out)]) == 0
    assert out.read_bytes()[:5] == b"%PDF-"


def bend_midi(curves, bend_range=None, mod=None):
    """4分音符を並べ、それぞれにピッチベンドの動き [(拍内の位置0-1, 値-8192..8191)] を付ける。"""
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    msgs = []
    if bend_range:
        msgs += [(0, mido.Message("control_change", control=101, value=0)),
                 (0, mido.Message("control_change", control=100, value=0)),
                 (0, mido.Message("control_change", control=6, value=bend_range))]
    for i, curve in enumerate(curves):
        t = i * 480
        msgs.append((t, mido.Message("pitchwheel", pitch=0)))
        msgs.append((t, mido.Message("note_on", note=64 + i, velocity=80)))
        msgs += [(t + int(pos * 480), mido.Message("pitchwheel", pitch=v)) for pos, v in curve]
        msgs.append((t + 470, mido.Message("note_off", note=64 + i)))
    for tick, value in mod or []:
        msgs.append((tick, mido.Message("control_change", control=1, value=value)))
    msgs.sort(key=lambda p: p[0])
    now = 0
    for tick, msg in msgs:
        track.append(msg.copy(time=tick - now))
        now = tick
    return mid


def test_bend_and_vibrato_detected_from_pitch_wheel():
    full, half = 8191, 4096
    vib = [(0.1 * k, 600 if k % 2 else -600) for k in range(1, 9)]
    score = m.analyze(bend_midi([
        [(0.2, half), (0.4, full)],                    # 1音チョーキング
        [(0.2, half)],                                 # 半音
        [(0.2, full), (0.6, half), (0.8, 0)],          # 上げて戻す
        vib,                                           # ビブラート
        [],                                            # 何もなし
    ]), grid=4)
    assert score.marks == {0: ["b2"], 1: ["b1"], 2: ["br2"], 3: ["vib"]}
    # ベンド幅を ±12 に設定したMIDI: 同じ値でも半音数が変わる
    wide = m.analyze(bend_midi([[(0.3, 1365)]], bend_range=12), grid=4)
    assert wide.marks == {0: ["b2"]}
    # モジュレーションホイールでもビブラート。--no-expression で無効
    modded = bend_midi([[], []], mod=[(480, 80), (900, 0)])
    assert m.analyze(modded, grid=4).marks == {1: ["vib"]}
    assert m.analyze(modded, grid=4, expression=False).marks == {}


def test_edit_marks_and_slides():
    import marks
    base = edited({})
    t0, t1 = str(base.tick(0)), str(base.tick(1))
    score = edited({"marks": {t0: ["vib", "pm", "b2", "br1", "bogus", "vib"], t1: ["x"]},
                    "techs": {t1: "sl"}})
    assert score.marks == {0: ["vib", "pm", "b2"], 1: ["x"]}   # 不明なコードと2つ目のベンドは捨てる
    assert score.techs == {1: ("sl", 0)}
    assert marks.parse_bend("pbr2") == ("pbr", 2.0) and marks.parse_bend("b7") is None
    # 自動判定されたマークを手動で消せる
    auto = bend_midi([[(0.3, 8191)]])
    assert m.analyze(auto, grid=4, edits={"marks": {"0": []}}).marks == {}


def test_all_marks_render(tmp_path):
    import xml.dom.minidom
    import marks
    import tabsvg
    notes = [(i * 0.5, [64 + i % 5], 0.5) for i in range(40)]
    base = m.analyze(make_midi(notes))
    codes = [c for c, _ in marks.MARKS] + [c for c, _ in marks.BENDS]
    edits = {"marks": {str(base.tick(s)): [codes[i % len(codes)]]
                       for i, s in enumerate(base.fingerings)},
             "techs": {str(base.tick(s)): list(marks.CONNECTIONS)[i % 5]
                       for i, s in enumerate(base.fingerings)}}
    score = m.analyze(make_midi(notes), edits=edits)
    assert {c for ms in score.marks.values() for c in ms} == set(codes)
    svgs = tabsvg.render_systems(score)
    for svg in svgs:
        xml.dom.minidom.parseString(svg)
    joined = "".join(svgs)
    for text in ("P.M.", "N.H.", "A.H.", "P.H.", ">X<", "full", "sl.", "&lt;"):
        assert text in joined, text


def test_editor_falls_back_when_default_port_is_taken():
    import socket
    import editor
    session = editor.Session(make_midi(LICK), {}, {}, None, "song")
    blocker = socket.socket()
    try:
        try:
            blocker.bind(("127.0.0.1", editor.DEFAULT_PORT))
            blocker.listen()
        except OSError:
            pass  # すでに誰かが使っている。どちらにせよ既定のポートは開けない
        server = editor.open_server(session, None)
        try:
            assert server.server_address[1] not in (0, editor.DEFAULT_PORT)
        finally:
            server.server_close()
        # 番号を明示したときは勝手に変えない
        import pytest
        with pytest.raises(OSError):
            editor.open_server(session, editor.DEFAULT_PORT)
    finally:
        blocker.close()


def raw_midi(notes, markers=()):
    """notes: [(開始tick, 長さtick, 音高)]、markers: [(tick, 名前)]"""
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    msgs = [(t, mido.MetaMessage("marker", text=text)) for t, text in markers]
    for on, length, pitch in notes:
        msgs.append((on, mido.Message("note_on", note=pitch, velocity=90)))
        msgs.append((on + length, mido.Message("note_off", note=pitch)))
    msgs.sort(key=lambda p: (p[0], p[1].type != "note_off"))
    now = 0
    for tick, msg in msgs:
        track.append(msg.copy(time=tick - now))
        now = tick
    return mid


def test_programmed_slide_down_becomes_one_mark():
    # オクターブの和音のあと、半音ずつ素早く下がる効果音が続く(声部ごとに1〜3tickずれている)
    notes = [(0, 190, 57), (0, 191, 69)]
    t = 190
    for i, gap in enumerate([37, 18, 17, 13, 7, 6]):
        notes += [(t, gap, 56 - i), (t + (i % 3), gap, 68 - i)]
        t += gap
    notes.append((960, 480, 60))
    score = m.analyze(raw_midi(notes), grid=16)
    assert score.warnings == []
    assert sorted(score.fingerings) == [0, 8]              # 効果音は譜面に出さない
    assert len(score.fingerings[0]) == 2
    assert score.marks == {0: ["so_down"]}
    # 自動判定を切ると、そのままの音として扱う
    assert len(m.analyze(raw_midi(notes), grid=16, expression=False).fingerings) > 2


def test_grace_note_becomes_slide_in():
    score = m.analyze(raw_midi([(0, 480, 60), (448, 32, 70), (480, 400, 69)]), grid=16)
    assert sorted(score.fingerings) == [0, 4]
    assert score.marks == {4: ["si_down"]}


def test_keyswitch_notes_ignored_or_shown_as_dead_strums():
    chord = [(0, 400, p) for p in (45, 53, 60, 64)]
    notes = chord + [(240, 60, 94), (360, 60, 93), (480, 400, 62)]
    ignored = m.analyze(raw_midi(notes), grid=16, dead_notes=[])
    assert sorted(ignored.fingerings) == [0, 4]
    assert "無視" in ignored.warnings[0] and "ノート番号93" in ignored.warnings[0]
    assert m.dead_note_list("none") == [] and m.dead_note_list("93,94") == [93, 94]

    # 指定しなければ、音域より高い音は自動で X になる
    auto = m.analyze(raw_midi(notes), grid=16)
    assert auto.marks == {2: ["x"], 3: ["x"]} and "ノート番号93" in auto.warnings[0]

    score = m.analyze(raw_midi(notes), grid=16, dead_notes=[93, 94])
    assert score.warnings == []
    assert sorted(score.fingerings) == [0, 2, 3, 4]
    assert score.fingerings[2] == score.fingerings[0] == score.fingerings[3]   # 同じ弦を X で
    assert score.marks == {2: ["x"], 3: ["x"]}
    assert score.chords == {0: "Fmaj7/A"}                  # X はコード判定に影響しない
    assert 0 not in score.ringing                          # 空ピッキングで音は止まる


def test_sections_from_markers_and_track_listing(tmp_path, capsys):
    mid = raw_midi([(1920, 480, 60), (3840, 480, 62)], markers=[(1915, "Verse"), (3900, "Chorus")])
    mid.tracks[0].insert(0, mido.MetaMessage("instrument_name", name="Lead_Gt2", time=0))
    mid.tracks[0].insert(0, mido.MetaMessage("track_name", name="Lead_Gt1", time=0))
    score = m.analyze(mid, grid=4)
    assert score.sections == {1: "Verse", 2: "Chorus"}     # 少しずれたマーカーも小節頭に寄せる
    import tabsvg
    assert "Verse" in "".join(tabsvg.render_systems(score))
    path = tmp_path / "s.mid"
    mid.save(path)
    assert m.main([str(path), "--list"]) == 0
    out = capsys.readouterr().out
    assert "Lead_Gt1 / Lead_Gt2" in out and "位置=4-8拍" in out


def test_two_hand_tapping_over_held_chord():
    # 6弦開放を含む低いコードを鳴らしっぱなしにした上に、高い音が短く入る。
    # 左手は届かないので、高い音は右手で叩く
    held = [(0, 1900, 40), (30, 1900, 47), (60, 1900, 52)]
    high = [(960, 200, 76), (1200, 200, 79)]
    mid = raw_midi(held + high + [(1920, 480, 40)])
    plain = m.analyze(mid, grid=8)
    tap = m.analyze(mid, grid=8, tapping=True)
    assert plain.tapped == set()
    assert tap.tapped == {4, 5}
    assert tap.fingerings[0] == ((0, 0), (1, 2), (2, 2))    # 左手はローポジションのまま
    assert tap.fingerings[4] == ((5, 12),) and tap.fingerings[5] == ((5, 15),)
    assert tap.techs[4][0] == "T" and tap.techs[5][0] == "T"


def test_melody_over_held_bass_shares_strings():
    # ベースを伸ばしたまま、重ならないメロディが動く: メロディは同じ弦を使ってよい
    notes = [(0, 1900, 45)] + [(i * 240, 230, p) for i, p in enumerate([64, 65, 67, 69, 67, 65, 64, 62])]
    score = m.analyze(raw_midi(notes), grid=8)
    assert score.warnings == []
    strings = [s for step in sorted(score.fingerings) for s, f in score.fingerings[step] if (s, f) != (1, 0)]
    assert score.fingerings[0][0] == (1, 0)                  # ベースは5弦開放
    assert 1 not in strings                                  # 鳴っているベースの弦は使わない
    frets = [f for step in sorted(score.fingerings) for s, f in score.fingerings[step] if s != 1 and f]
    assert max(frets) - min(frets) <= 4
