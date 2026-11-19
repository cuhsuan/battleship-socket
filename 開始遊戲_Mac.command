#!/bin/bash
cd "$(dirname "$0")"
python3 client_gui.py || { echo; echo "啟動失敗：請先安裝 Python 3（python.org）"; read -p "按 Enter 關閉"; }
