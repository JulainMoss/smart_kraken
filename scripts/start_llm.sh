#!/usr/bin/env bash
# Lokalny model językowy dla Risk Miner: serwer llama.cpp z API zgodnym z OpenAI (http://127.0.0.1:8080).
# Użycie: scripts/start_llm.sh [plik_modelu.gguf]   (domyślnie Bielik-1.5B-v3.0-Instruct Q8_0)
# Pliki: LLM_DIR (domyślnie ../llm_runtime obok repozytorium) z podkatalogami bin/ (llama.cpp) i models/ (GGUF).
set -euo pipefail
DIR="${LLM_DIR:-$(cd "$(dirname "$0")/../.." && pwd)/llm_runtime}"
MODEL="${1:-bielik-1.5b-q8.gguf}"   # Bielik 1.5B (SpeakLeash, Apache 2.0); alternatywa: qwen2.5-3b-q4km.gguf
SERVER="$(ls "$DIR"/bin/*/llama-server 2>/dev/null | head -1)"
[ -x "$SERVER" ] || { echo "Brak llama-server w $DIR/bin – pobierz: https://github.com/ggml-org/llama.cpp/releases (ubuntu-x64)"; exit 1; }
[ -f "$DIR/models/$MODEL" ] || { echo "Brak modelu $DIR/models/$MODEL"; exit 1; }
THREADS="${THREADS:-$(( $(nproc) > 4 ? $(nproc) - 2 : $(nproc) ))}"
echo "Start: $MODEL na http://127.0.0.1:8080 (wątki: $THREADS)"
exec "$SERVER" -m "$DIR/models/$MODEL" --alias "${MODEL%.gguf}" --host 127.0.0.1 --port 8080 -c 8192 -t "$THREADS" --no-webui
