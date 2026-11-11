"""HTMLの譜面をPDFにする。Chrome系ブラウザのヘッドレス印刷を使う。"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile

BROWSER_NAMES = ["chrome-headless-shell", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "chrome", "msedge", "microsoft-edge", "brave-browser"]
BROWSER_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


class PdfError(Exception):
    pass


def find_browser() -> str | None:
    """環境変数 CHROME_PATH、PATH 上のコマンド、よくあるインストール先の順に探す。"""
    env = os.environ.get("CHROME_PATH")
    if env and os.path.exists(env):
        return env
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return next((p for p in BROWSER_PATHS if os.path.exists(p)), None)


def html_to_pdf(html: str) -> bytes:
    browser = find_browser()
    if not browser:
        raise PdfError("PDF出力には Chrome / Edge / Chromium が必要です。"
                       "見つからない場合は環境変数 CHROME_PATH に実行ファイルの場所を指定するか、"
                       "HTMLで出力してブラウザの「印刷 → PDFに保存」を使ってください")
    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / "tab.html"
        out = pathlib.Path(tmp) / "tab.pdf"
        src.write_text(html, encoding="utf-8")
        cmd = [browser, "--headless", "--disable-gpu", "--no-pdf-header-footer",
               "--print-to-pdf-no-header", f"--user-data-dir={tmp}/profile",
               f"--print-to-pdf={out}", src.as_uri()]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.insert(1, "--no-sandbox")  # rootではサンドボックスが使えない
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise PdfError(f"ブラウザを実行できませんでした: {e}") from None
        if not out.exists():
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
            raise PdfError("PDFを生成できませんでした" + (f": {detail[0]}" if detail else ""))
        return out.read_bytes()
