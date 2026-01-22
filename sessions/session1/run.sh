#!/bin/bash
# =============================================================================
# Session 1 - Run Script (Versão Compartilhada)
# =============================================================================

cd "$(dirname "$0")"

# Carregar variáveis
export $(grep -v '^#' .env | xargs)

# Ativar venv (assumindo que está no diretório pai)
source ../../venv/bin/activate 2>/dev/null || source ../venv/bin/activate 2>/dev/null || source ~/venv/bin/activate

# Executar versão compartilhada
echo "🚀 Iniciando Session 1 (Shared Checkpoint)..."
echo "   Session: $SESSION_NAME"
echo "   Source: $SOURCE_CHAT"
echo "   Target: $TARGET_CHAT"
echo "   DB: $SHARED_DB_PATH"
python ../../clone_streaming_shared.py
