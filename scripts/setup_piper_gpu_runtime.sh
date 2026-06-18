#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${PIPER_GPU_RUNTIME_DIR:-$HOME/piper-gpu}"
VENV_DIR="$RUNTIME_DIR/venv"
BIN_DIR="$RUNTIME_DIR/bin"
SITE_DIR="$RUNTIME_DIR/site"
WRAPPER="$BIN_DIR/piper"

mkdir -p "$BIN_DIR"
if python3 -m venv "$VENV_DIR"; then
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install piper-tts onnxruntime-gpu
  "$VENV_DIR/bin/python" -m pip install --upgrade --force-reinstall onnxruntime-gpu
  cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python" "$ROOT_DIR/tools/piper_gpu_wrapper.py" "\$@"
EOF
else
  rm -rf "$VENV_DIR"
  python3 -m pip install --upgrade --target "$SITE_DIR" piper-tts onnxruntime-gpu
  python3 -m pip install --upgrade --force-reinstall --target "$SITE_DIR" onnxruntime-gpu
  cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$SITE_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 "$ROOT_DIR/tools/piper_gpu_wrapper.py" "\$@"
EOF
fi
chmod +x "$WRAPPER"

echo "$WRAPPER"
