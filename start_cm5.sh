#!/bin/sh
# 使用本项目的Python环境；可通过环境变量指定已经安装的环境。
set -eu
EYES_ENTRY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
EYES_PYTHON=${EYES_CM5_PYTHON:-"$EYES_ENTRY_DIR/.venv/bin/python"}
if [ ! -x "$EYES_PYTHON" ]; then
    echo "现有语音Python环境不存在，请核对路径；未自动安装。" >&2
    exit 1
fi
export PYTHONPYCACHEPREFIX="$EYES_ENTRY_DIR/.runtime/pycache"
exec "$EYES_PYTHON" "$EYES_ENTRY_DIR/run_cm5.py" "$@"
