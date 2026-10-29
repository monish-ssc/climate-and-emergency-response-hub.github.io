#!/usr/bin/env bash
# Start Ollama (open source) on a GitHub Actions runner and prepare the model for the AI briefings.
# Used by refresh-live-data.yml and generate-replay-briefings.yml.
# Needs BRIEFING_BASE_MODEL (e.g. qwen2.5:7b) and BRIEFING_MODELS (the model build_briefings.py calls:
# the same model with a longer context window, created here).
set -euo pipefail

curl -fsSL https://ollama.com/install.sh | sh

# The installer starts a system service that keeps models in /usr/share/ollama. Run our own server instead,
# with the models in ~/.ollama/models, which the workflow restores from the Actions cache.
sudo systemctl stop ollama 2>/dev/null || true
export OLLAMA_MODELS="$HOME/.ollama/models"
mkdir -p "$OLLAMA_MODELS"
nohup ollama serve > "${RUNNER_TEMP:-/tmp}/ollama.log" 2>&1 &
for _ in $(seq 1 60); do
  curl -sf http://localhost:11434/api/tags > /dev/null && break
  sleep 1
done
ollama --version

ollama pull "$BRIEFING_BASE_MODEL"
# Ollama's default context window is too short for the instructions, the facts and a full answer.
printf 'FROM %s\nPARAMETER num_ctx 8192\n' "$BRIEFING_BASE_MODEL" > "${RUNNER_TEMP:-/tmp}/Modelfile"
ollama create "${BRIEFING_MODELS%%,*}" -f "${RUNNER_TEMP:-/tmp}/Modelfile"
ollama list
