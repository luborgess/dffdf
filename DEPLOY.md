# 🚀 Manual de Deploy - TelePi VPS Oracle

## 📋 Informações da VPS

| Campo | Valor |
|-------|-------|
| **IP** | `158.101.105.236` |
| **Usuário** | `ubuntu` |
| **Chave SSH** | `oracle_vps.pem` |
| **OS** | Ubuntu 22.04 LTS |
| **Shape** | VM.Standard.E5.Flex (2 OCPU, 6GB RAM) |

---

## 🔐 Conexão SSH

```powershell
# Windows PowerShell
ssh -i ".\oracle_vps.pem" ubuntu@158.101.105.236
```

```bash
# Linux/Mac
ssh -i ./oracle_vps.pem ubuntu@158.101.105.236
```

---

## 📁 Estrutura na VPS

```
/home/ubuntu/
├── clone_streaming.py    # Script principal
├── requirements.txt      # Dependências Python
├── watermark.png         # Watermark para vídeos
├── .env                  # Variáveis de ambiente (criar manualmente)
├── venv/                 # Ambiente virtual Python
├── scripts/
│   ├── setup.sh
│   └── network-tuning.sh
├── checkpoint.txt        # Progresso do clone (gerado automaticamente)
└── clone.log             # Logs de execução
```

---

## 🔄 Deploy de Alterações

### Atualizar Script Principal

```powershell
# Copiar clone_streaming.py atualizado
scp -i ".\oracle_vps.pem" clone_streaming.py ubuntu@158.101.105.236:~
```

### Atualizar Múltiplos Arquivos

```powershell
# Copiar vários arquivos de uma vez
scp -i ".\oracle_vps.pem" clone_streaming.py requirements.txt ubuntu@158.101.105.236:~
```

### Atualizar Dependências

```powershell
# Após atualizar requirements.txt
ssh -i ".\oracle_vps.pem" ubuntu@158.101.105.236 "source venv/bin/activate && pip install -r requirements.txt"
```

### Atualizar Watermark

```powershell
scp -i ".\oracle_vps.pem" watermark.png ubuntu@158.101.105.236:~
```

---

## ⚙️ Configuração do .env

### Criar arquivo .env na VPS

```powershell
ssh -i ".\oracle_vps.pem" ubuntu@158.101.105.236
```

Depois, na VPS:

```bash
cat > ~/.env << 'EOF'
TG_API_ID="SEU_API_ID"
TG_API_HASH="SEU_API_HASH"
SOURCE_CHAT="-100XXXXXXXXXX"
TARGET_CHAT="-100XXXXXXXXXX"
SOURCE_TOPIC=""
TARGET_TOPIC=""
AUTO_CREATE_TOPICS="true"
EOF
```

### Ou copiar .env local (se existir)

```powershell
scp -i ".\oracle_vps.pem" .env ubuntu@158.101.105.236:~
```

---

## ▶️ Executar o Script

### Modo Interativo (com logs na tela)

```bash
# Conectar na VPS
ssh -i ".\oracle_vps.pem" ubuntu@158.101.105.236

# Ativar ambiente e executar
source venv/bin/activate
export $(grep -v '^#' .env | xargs)
python clone_streaming.py
```

### Modo Background (continua após desconectar)

```bash
# Na VPS
source venv/bin/activate
export $(grep -v '^#' .env | xargs)
nohup python clone_streaming.py > output.log 2>&1 &

# Ver processo
ps aux | grep clone_streaming

# Ver logs em tempo real
tail -f output.log
```

### Usando Screen (recomendado)

```bash
# Instalar screen (primeira vez)
sudo apt install -y screen

# Criar sessão
screen -S telepi

# Executar
source venv/bin/activate
export $(grep -v '^#' .env | xargs)
python clone_streaming.py

# Desanexar: Ctrl+A, depois D

# Reconectar depois
screen -r telepi
```

---

## 🛑 Parar Execução

```bash
# Ver processos Python
ps aux | grep python

# Matar processo específico
kill <PID>

# Ou matar todos os processos Python
pkill -f clone_streaming.py
```

---

## 📊 Monitoramento

### Ver Logs

```bash
# Logs do script
tail -f ~/clone.log

# Logs de output (se rodando com nohup)
tail -f ~/output.log
```

### Ver Checkpoint (progresso)

```bash
cat ~/checkpoint.txt
```

### Ver Uso de Recursos

```bash
# Memória e CPU
htop

# Espaço em disco
df -h

# Conexões de rede
ss -tunp | grep python
```

---

## 🔧 Setup Inicial (primeira vez)

Se precisar refazer o setup completo:

```bash
# Na VPS
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv ffmpeg

# Criar venv
python3 -m venv venv
source venv/bin/activate

# Instalar dependências
pip install --upgrade pip
pip install -r requirements.txt

# Aplicar tuning de rede
sudo bash scripts/network-tuning.sh
```

---

## 🔑 Sessão do Telegram

Na primeira execução, o script pedirá para autenticar no Telegram:
1. Digite seu número de telefone
2. Digite o código recebido no Telegram
3. O arquivo `session.session` será criado

Para copiar sessão existente:

```powershell
scp -i ".\oracle_vps.pem" *.session ubuntu@158.101.105.236:~
```

---

## ❌ Troubleshooting

### Erro de conexão SSH
```powershell
# Verificar se a chave tem permissões corretas
icacls .\oracle_vps.pem /inheritance:r /grant:r "$($env:USERNAME):(R)"
```

### Erro de permissão no script
```bash
chmod +x scripts/*.sh
```

### Erro de dependências Python
```bash
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt --force-reinstall
```

### Processo travado
```bash
# Ver se está rodando
ps aux | grep clone

# Matar e reiniciar
pkill -f clone_streaming.py
# ... reiniciar ...
```

---

## 🗑️ Limpar e Recomeçar

```bash
# Remover checkpoint (recomeça do zero)
rm ~/checkpoint.txt

# Remover logs
rm ~/clone.log ~/output.log

# Remover sessão (precisará autenticar novamente)
rm ~/*.session
```

---

## 📌 Comandos Rápidos

```powershell
# Conectar
ssh -i ".\oracle_vps.pem" ubuntu@158.101.105.236

# Deploy rápido
scp -i ".\oracle_vps.pem" clone_streaming.py ubuntu@158.101.105.236:~

# Ver status
ssh -i ".\oracle_vps.pem" ubuntu@158.101.105.236 "ps aux | grep python; tail -5 clone.log"

# Reiniciar script
ssh -i ".\oracle_vps.pem" ubuntu@158.101.105.236 "pkill -f clone_streaming.py; source venv/bin/activate && export \$(grep -v '^#' .env | xargs) && nohup python clone_streaming.py > output.log 2>&1 &"
```
