# 📁 Multi-Session Structure (Checkpoint Compartilhado)

Versão com **SQLite compartilhado** para coordenação entre múltiplas sessões.

**Benefícios:**
- ✅ Sem duplicação de mensagens no target
- ✅ Múltiplas sessões processando em paralelo
- ✅ Lock atômico por mensagem
- ✅ Recuperação de falhas automática

Cada sessão tem seu próprio diretório isolado com:
- `.env` - Credenciais e configurações
- `*.session` - Arquivo de sessão Telegram (gerado na autenticação)
- `clone.log` - Logs de execução
- `topic_map.json` - Mapeamento de tópicos

## 🏗️ Estrutura

```
sessions/
├── shared/
│   └── checkpoint.db     # Banco SQLite COMPARTILHADO
│
├── session1/             # Conta Telegram A
│   ├── .env              # SOURCE_CHAT diferente
│   ├── run.sh            # Script de execução
│   ├── session1.session  # (gerado)
│   └── clone.log         # (gerado)
│
├── session2/             # Conta Telegram B
│   ├── .env              # SOURCE_CHAT diferente
│   ├── run.sh            # Script de execução
│   ├── session2.session  # (gerado)
│   └── clone.log         # (gerado)
│
└── README.md
```

## 🚀 Setup

### 1. Configurar cada sessão

```bash
# Session 1
cp session1/.env.example session1/.env
nano session1/.env  # Configurar credenciais e SOURCE_CHAT

# Session 2
cp session2/.env.example session2/.env
nano session2/.env  # Configurar credenciais e SOURCE_CHAT diferente
```

### 2. Executar

```bash
# Terminal 1
cd session1 && bash run.sh

# Terminal 2
cd session2 && bash run.sh
```

### Ou com Screen (recomendado)

```bash
# Criar sessões screen
screen -S telepi1 -dm bash -c "cd session1 && bash run.sh"
screen -S telepi2 -dm bash -c "cd session2 && bash run.sh"

# Ver sessões
screen -ls

# Conectar em uma sessão
screen -r telepi1
```

## 🔄 Como Funciona o Checkpoint Compartilhado

```
Session 1                      SQLite DB                     Session 2
    │                             │                              │
    ├─ Msg 100: lock? ──────────►│                              │
    │           ◄─── OK, locked ─┤                              │
    │                             │◄───── Msg 100: lock? ───────┤
    │                             ├─── DENIED (já em processo) ─►│
    │  [processando...]           │                   [pula] ────┤
    │                             │◄───── Msg 101: lock? ───────┤
    │                             ├─── OK, locked ──────────────►│
    ├─ Msg 100: done ───────────►│                              │
    │                             │                  [processando]
    ├─ Msg 102: lock? ──────────►│                              │
    ...                           │                             ...
```

## ⚡ Vantagens desta estrutura

1. **Sem duplicação** - Cada mensagem é processada uma única vez
2. **Rate limits independentes** - Cada conta tem seu próprio flood wait
3. **Sessões Telegram isoladas** - Arquivos .session não conflitam
4. **Checkpoint atômico** - SQLite garante consistência
5. **Recuperação de falhas** - Locks antigos são liberados automaticamente
6. **Fácil escalar** - Adicionar session3, session4, etc.

## 📊 Monitorar Checkpoint

```bash
# Ver estatísticas do banco
sqlite3 sessions/shared/checkpoint.db "SELECT status, COUNT(*) FROM messages GROUP BY status;"

# Ver mensagens em processamento
sqlite3 sessions/shared/checkpoint.db "SELECT * FROM messages WHERE status='processing';"

# Ver últimas mensagens processadas
sqlite3 sessions/shared/checkpoint.db "SELECT * FROM messages ORDER BY processed_at DESC LIMIT 10;"
```

## ⚠️ Importante

- Ambas as sessões podem escrever no **mesmo TARGET_CHAT**
- A ordem das mensagens é **por quem processar primeiro**
- Cada sessão usa seu próprio **SOURCE_CHAT** ou podem usar o mesmo
- Se usar o **mesmo SOURCE_CHAT**, as mensagens serão divididas entre sessões

## 🆚 Quando usar cada versão

| Cenário | Script |
|---------|--------|
| Uma sessão, um source | `clone_streaming.py` |
| Múltiplas sessões, sources diferentes, targets diferentes | `clone_streaming.py` (separado) |
| Múltiplas sessões, **mesmo target**, sem duplicação | `clone_streaming_shared.py` ✅ |
| Múltiplas sessões, **mesmo source**, dividir trabalho | `clone_streaming_shared.py` ✅ |

## 🔄 Deploy na VPS

```powershell
# Copiar estrutura
scp -i ".\oracle_vps.pem" -r sessions ubuntu@158.101.105.236:~
scp -i ".\oracle_vps.pem" clone_streaming_shared.py ubuntu@158.101.105.236:~

# Na VPS, configurar cada .env
ssh -i ".\oracle_vps.pem" ubuntu@158.101.105.236
cd ~/sessions/session1
cp .env.example .env
nano .env

cd ~/sessions/session2
cp .env.example .env
nano .env
```
