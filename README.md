# TelePi - Telegram Streaming Cloner

Clone de grupos Telegram com **streaming real** - processa arquivos de até 2GB sem sobrecarregar memória ou disco.

## 🚀 Arquitetura

```
TELEGRAM           EC2 c6in.xlarge            TELEGRAM
(origem)           RAM Buffer ~500MB          (destino)
    │                     │                        │
    │   chunk 1 (512KB)   │                        │
    │────────────────────►│  saveBigFilePart       │
    │                     │───────────────────────►│
    │   chunk 2 (512KB)   │                        │
    │────────────────────►│───────────────────────►│
    │        ...          │        ...             │
    │   chunk N           │  sendMedia()           │
    │────────────────────►│───────────────────────►│

    NUNCA TEM 2GB EM RAM/DISCO - MÁXIMO ~500MB DE BUFFER
```

## ✨ Funcionalidades

- **Streaming Real**: Upload em paralelo enquanto faz download
- **Topic Mirroring**: Cria automaticamente tópicos no destino com mesmo nome da origem
- **Resume Capability**: Continua de onde parou em caso de falha (checkpoint)
- **Zero Disk**: Não usa disco para arquivos (exceto como fallback/swap se necessário)

## 📊 Performance

| Método | Tempo (2GB) | RAM Max | Disco |
|--------|-------------|---------|-------|
| Download → Upload | ~7 min | 2+ GB | 2 GB |
| Download → Disco → Upload | ~6 min | ~100 MB | 2 GB |
| **Streaming Real** | **~4 min** | **~500 MB** | **0 GB** |

## 🛠️ Requisitos

- Python 3.12+
- Telegram API credentials ([obter aqui](https://my.telegram.org/apps))
- AWS CLI (para provisionamento de infraestrutura)

## ⚡ Quick Start

### 1. Criar Instância EC2

```bash
# Configurar AWS CLI (se necessário)
aws configure

# Criar instância (c6in.xlarge - $140/mês)
chmod +x scripts/aws-create-instance.sh
./scripts/aws-create-instance.sh
```

### 2. Deploy no EC2

```bash
# Copiar arquivos para EC2
scp -i telepi-key.pem -r . ubuntu@<IP>:~/telepi

# Conectar
ssh -i telepi-key.pem ubuntu@<IP>

# Setup
cd telepi
chmod +x scripts/*.sh
sudo ./scripts/network-tuning.sh
./scripts/setup.sh
```

### 3. Configurar

```bash
# Copiar e editar .env
cp .env.example .env
nano .env
```

Variáveis necessárias:
```bash
TG_API_ID="123456"           # Seu API ID
TG_API_HASH="abcdef..."      # Seu API Hash
SOURCE_CHAT="-100123456789"  # Chat de origem
TARGET_CHAT="-100987654321"  # Chat de destino
SOURCE_TOPIC=""              # Tópico origem (opcional)
TARGET_TOPIC=""              # Tópico destino (opcional)
```

### 4. Executar

```bash
# Ativar ambiente
source venv/bin/activate

# Carregar variáveis
export $(grep -v '^#' .env | xargs)

# Executar
python clone_streaming.py

# Executar em background
nohup python clone_streaming.py > output.log 2>&1 &
```

## 📁 Estrutura

```
telepi/
├── clone_streaming.py    # Script principal
├── requirements.txt      # Dependências Python
├── .env.example          # Template de configuração
├── .gitignore
└── scripts/
    ├── aws-create-instance.sh  # Provisiona EC2
    ├── network-tuning.sh       # Tuning de rede
    └── setup.sh                # Setup de ambiente
```

## 💰 Custos AWS

| Item | Custo/mês |
|------|-----------|
| EC2 c6in.xlarge | ~$140 |
| EBS gp3 50GB | ~$4 |
| Data Transfer | ~$10 |
| **Total** | **~$154** |

**Alternativa econômica:** EC2 c6in.large = ~$84/mês

## 📝 Logs

- Console: tempo real
- Arquivo: `clone.log`
- Checkpoint: `checkpoint.txt` (para retomada)

## ⚠️ Rate Limits

O Telegram impõe limite de ~20 mensagens/minuto. O script aguarda automaticamente 3.5s entre mensagens.

## 📄 Licença

MIT
