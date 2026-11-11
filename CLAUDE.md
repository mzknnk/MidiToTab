# midi2tab

ギターのMIDIをTAB譜(ASCII / HTML / PDF)に書き起こすツール。README.md に使い方と制限がある。

## 構成

- `midi2tab.py` — MIDI読み込み、クオンタイズ、運指の決定(DP)、リズム分解、手動編集の反映、CLI
- `chords.py` — コードネーム判定
- `marks.py` — 奏法記号の定義と、ピッチベンドからの自動判定
- `tabsvg.py` — 譜面のSVG/HTML描画
- `editor.py` — 編集画面(ローカルHTTPサーバー + 1ページのUI)
- `pdfout.py` — ヘッドレスChromeによるPDF化
- `tests/test_midi2tab.py` — テスト

## 開発

```bash
pip install -r requirements.txt
python3 -m pytest -q
```

- PDFのテストは Chrome 系ブラウザが無いとスキップされる。見つからない/固まる場合は
  `CHROME_PATH` にヘッドレス用の実行ファイル(chrome-headless-shell など)を指定する。
- 描画を変えたら `examples/` のMIDIをHTMLに出力し、スクリーンショットで見た目を確認する。
- コメントとメッセージは日本語。
