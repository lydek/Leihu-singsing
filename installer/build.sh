#!/bin/sh
# 建立 Windows 安裝檔：需要 NSIS（makensis）。在 repo 根目錄執行 sh installer/build.sh
set -e
cd "$(dirname "$0")"
rm -rf payload && mkdir -p payload/static payload/setup out
cp ../app.py ../desktop.py ../installer.py ../pipeline.py ../claude_api.py ../share.py ../score_sheet.py ../hotkeys.py ../gpu_worker.py ../exporter.py ../VERSION ../requirements-win.txt payload/
mkdir -p payload/leihu/web && cp ../leihu/__init__.py payload/leihu/ && cp ../leihu/web/*.py payload/leihu/web/
cp ../static/index.html ../static/app.js ../static/style.css ../static/help.html ../static/overlay.html ../static/timing.html ../static/icon.ico ../static/kenzi.png ../static/linepay.png ../static/song-list-template.csv payload/static/
mkdir -p payload/static/fonts && cp ../static/fonts/* payload/static/fonts/
cp ../static/help.html payload/使用說明.html
cp ../setup/install.ps1 payload/setup/
cp license.txt payload/版權與使用聲明.txt
# -INPUTCHARSET UTF8：leihu.nsi 是不含 BOM 的 UTF-8；Windows 版 makensis 不指定時用系統的 ANSI 編碼讀（Bad text encoding）
# LEIHU_TEST=1：建測試版（名稱、安裝位置、登錄檔和正式版分開，見 leihu.nsi 的 TEST_BUILD）
DEFS=""
if [ "${LEIHU_TEST:-}" = "1" ]; then DEFS="-DTEST_BUILD"; fi
LANG=C.UTF-8 LC_ALL=C.UTF-8 makensis -V2 -INPUTCHARSET UTF8 $DEFS leihu.nsi
ls -la out
