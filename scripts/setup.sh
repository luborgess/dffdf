#!/bin/bash
# =============================================================================
# TelePi - Setup Script para EC2
# Instala dependências e configura ambiente
# =============================================================================

set -e

echo "🚀 TelePi Setup"
echo "============================================================"

# Atualizar sistema
echo "📦 Atualizando sistema..."
sudo apt update && sudo apt upgrade -y

# Instalar Python 3.12 e pip
echo "🐍 Instalando Python..."
sudo apt install -y python3.12 python3-pip python3.12-venv

# Criar virtual environment
echo "📁 Criando ambiente virtual..."
python3.12 -m venv venv
source venv/bin/activate

# Instalar dependências
echo "📚 Instalando dependências Python..."
pip install --upgrade pip
pip install -r requirements.txt

# Verificar instalação
echo ""
echo "✅ Setup concluído!"
echo ""
python3 --version
pip show telethon | grep -E "^(Name|Version)"

echo ""
echo "============================================================"
echo "📋 PRÓXIMOS PASSOS:"
echo "============================================================"
echo ""
echo "1. Copie .env.example para .env e configure:"
echo "   cp .env.example .env"
echo "   nano .env"
echo ""
echo "2. Configure as variáveis de ambiente:"
echo "   export \$(grep -v '^#' .env | xargs)"
echo ""
echo "3. Execute o cloner:"
echo "   source venv/bin/activate"
echo "   python clone_streaming.py"
echo ""
echo "4. Para rodar em background:"
echo "   nohup python clone_streaming.py > output.log 2>&1 &"
echo ""
echo "============================================================"
