#!/usr/bin/env python3
"""
Verifica configuração do TelePi antes de rodar
"""
import sys
import os

print("🔍 Verificando configuração do TelePi...\n")

# 1. Verificar Python
print(f"✓ Python: {sys.version}")

# 2. Verificar módulos
errors = []
try:
    import telethon
    print(f"✓ Telethon: {telethon.__version__}")
except ImportError as e:
    errors.append(f"✗ Telethon não instalado: {e}")

try:
    import cryptg
    print(f"✓ cryptg instalado")
except ImportError:
    print(f"⚠️  cryptg não instalado (opcional, mas recomendado para performance)")

try:
    import boto3
    print(f"✓ boto3 instalado")
except ImportError:
    print(f"⚠️  boto3 não instalado (necessário apenas para AWS)")

# 3. Verificar arquivo .env
print("\n📋 Variáveis de ambiente:")
if not os.path.exists('.env'):
    errors.append("✗ Arquivo .env não encontrado! Copie .env.example para .env")
else:
    print("✓ Arquivo .env existe")

# 4. Verificar variáveis obrigatórias
required_vars = ['TG_API_ID', 'TG_API_HASH', 'SOURCE_CHAT', 'TARGET_CHAT']
missing = []

for var in required_vars:
    value = os.environ.get(var)
    if value:
        # Mascarar valores sensíveis
        if 'HASH' in var:
            masked = value[:4] + '****' + value[-4:] if len(value) > 8 else '****'
            print(f"✓ {var}={masked}")
        else:
            print(f"✓ {var}={value}")
    else:
        missing.append(var)
        print(f"✗ {var} não configurado")

if missing:
    errors.append(f"✗ Variáveis faltando: {', '.join(missing)}")

# 5. Validar valores
if os.environ.get('TG_API_ID'):
    try:
        int(os.environ['TG_API_ID'])
        print("✓ TG_API_ID é um número válido")
    except ValueError:
        errors.append("✗ TG_API_ID deve ser um número")

for chat_var in ['SOURCE_CHAT', 'TARGET_CHAT']:
    if os.environ.get(chat_var):
        try:
            int(os.environ[chat_var])
            print(f"✓ {chat_var} é um número válido")
        except ValueError:
            errors.append(f"✗ {chat_var} deve ser um número")

# Resultado final
print("\n" + "="*60)
if errors:
    print("❌ PROBLEMAS ENCONTRADOS:\n")
    for error in errors:
        print(error)
    print("\n📖 Para corrigir:")
    print("1. Copie o arquivo: cp .env.example .env")
    print("2. Edite .env com suas credenciais")
    print("3. No PowerShell, carregue as variáveis:")
    print("   Get-Content .env | ForEach-Object { if ($_ -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim('\"'), 'Process') } }")
    sys.exit(1)
else:
    print("✅ TUDO OK! Pronto para rodar:")
    print("   python clone_streaming.py")
    sys.exit(0)
