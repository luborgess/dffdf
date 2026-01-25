# Script de Scan - Mídias por Usuário

## Visão Geral

O script `scan_users.py` analisa todas as mensagens de um chat do Telegram e gera estatísticas detalhadas sobre quantas mídias cada usuário possui. Isso é útil para identificar os maiores acervos e priorizar quais usuários processar primeiro.

## Funcionalidades

- ✅ Contagem total de mídias por usuário
- ✅ Detalhamento por tipo (vídeos, fotos, documentos)
- ✅ Contagem de álbuns
- ✅ Tamanho total (em bytes/MB/GB)
- ✅ Datas de primeira e última aparição
- ✅ Detecção automática de usernames em legendas
- ✅ Suporte a checkpoint (retomar scan interrompido)
- ✅ Relatórios em JSON e texto formatado
- ✅ **Uso de sessão existente (não precisa autenticar novamente)**

## Como Usar

### Opção 1: Usar Sessão Existente (Recomendado)

Se você já tem uma sessão configurada (ex: sessions/session1):

```bash
python3 scan_users.py
```

O script automaticamente:
- Detecta a pasta de sessão (sessions/session1)
- Usa o arquivo .session já existente
- Lê configurações de sessions/session1/.env
- Salva resultados na pasta da sessão

### Opção 2: Criar Nova Sessão

Se preferir criar uma nova sessão específica para scan:

1. Crie arquivo `.env` no diretório raiz:

```bash
TG_API_ID=seu_api_id
TG_API_HASH=seu_api_hash
SESSION_NAME=scanner
SOURCE_CHAT=id_do_chat_origem
SOURCE_TOPIC=id_do_topico_opcional  # opcional
```

2. Execute o scan (primeira vez pedirá autenticação):

```bash
python3 scan_users.py
```

### Arquivos Gerados

Os arquivos são salvos na pasta da sessão (ex: sessions/session1/):

#### `scan_media_by_user.json`
Arquivo JSON completo com todas as estatísticas:
```json
{
  "scan_date": "2026-01-22 17:00:00",
  "source_chat": -1001234567890,
  "total_users": 150,
  "total_media": 5686,
  "total_albums": 694,
  "no_username": 123,
  "users": {
    "usuario1": {
      "total_media": 450,
      "videos": 320,
      "photos": 120,
      "documents": 10,
      "albums": 85,
      "total_bytes": 15728640000,
      "first_seen": "2025-01-01T00:00:00",
      "last_seen": "2026-01-22T12:00:00"
    }
  }
}
```

#### `scan_report.txt` (na pasta da sessão)
Relatório em texto formatado com:
- Resumo geral
- TOP 50 usuários por quantidade de mídias
- TOP 10 usuários por tamanho total

#### `scan.log` (no diretório raiz)
Log de execução com progresso detalhado

### Interpretar os Resultados

**Colunas do Relatório:**
- `#`: Posição no ranking
- `Usuário`: Username identificado
- `Mídia`: Total de arquivos de mídia
- `Vídeo`: Quantidade de vídeos
- `Foto`: Quantidade de fotos
- `Doc`: Quantidade de documentos
- `Álbuns`: Quantidade de álbuns
- `Tamanho`: Tamanho total em MB ou GB

## Detecção de Usernames

O script usa múltiplos padrões para extrair usernames de legendas:

1. `⭐ » Username` ou `★ » Username`
2. `@username`
3. `Username - Onlyfans` ou `Username | Onlyfans`
4. `Onlyfans: Username` ou `OF: Username`
5. Username no início seguido de emoji ou quebra de linha

Se um username não é encontrado, a mídia é contabilizada na categoria "Mídias sem username".

## Checkpoint

O script salva automaticamente um checkpoint (`scan_checkpoint.txt` na pasta da sessão) a cada 1000 mensagens processadas. Se o scan for interrompido, você pode simplesmente rodar o script novamente e ele continuará de onde parou.

Após o scan completo, o checkpoint é automaticamente removido.

## Exemplos de Uso

### Exemplo 1: Verificar Maiores Acervos

```bash
python3 scan_users.py
cat sessions/session1/scan_report.txt
```

### Exemplo 2: Analisar Dados com Python

```python
import json

with open('sessions/session1/scan_media_by_user.json', 'r') as f:
    data = json.load(f)

# Top 10 usuários por quantidade
sorted_users = sorted(
    data['users'].items(),
    key=lambda x: x[1]['total_media'],
    reverse=True
)

for username, stats in sorted_users[:10]:
    print(f"{username}: {stats['total_media']} mídias ({stats['total_bytes']/1024**3:.2f} GB)")
```

### Exemplo 3: Usar com Script Principal

Após o scan, você pode usar os dados para filtrar usuários específicos no `clone_streaming.py`:

```bash
# Processar apenas o usuário com maior acervo
cd sessions/session1
export FILTER_USER="usuario1"
bash run.sh
```

### Exemplo 4: Scan com Sessão Específica

Se você tem múltiplas sessões e quer usar uma específica:

```bash
# Criar .env temporário
cat > .env << EOF
TG_API_ID=seu_api_id
TG_API_HASH=seu_api_hash
SESSION_NAME=session2
SOURCE_CHAT=id_do_chat_origem
EOF

python3 scan_users.py
```

## Diferença entre Modo Scan e Clone

| Característica | `scan_users.py` | `clone_streaming.py` |
|----------------|-----------------|----------------------|
| Baixa arquivos? | Não | Sim |
| Gera relatório? | Sim | Não |
| Organiza por usuário? | Apenas conta | Sim (envia para tópicos) |
| Tempo de execução | Rápido (só leitura) | Lento (download + upload) |
| Uso de rede | Mínimo | Alto |
| Usa sessão existente | ✅ Sim | ✅ Sim |

## Performance

Para um chat com ~5686 mensagens:
- **Tempo estimado**: 2-5 minutos
- **Uso de rede**: Mínimo (apenas metadados)
- **Uso de disco**: ~1-2 MB (logs + resultados)

## Troubleshooting

### Scan não encontra usuários
- Verifique se os padrões de regex correspondem ao formato das suas legendas
- Revise o arquivo `scan_report.txt` para ver quantas mídias não tiveram username identificado

### Erro de permissão
- Certifique-se de que o script tem permissão para escrever na pasta:
  ```bash
  chmod +x scan_users.py
  ```

### Scan muito lento
- O script é otimizado para velocidade, mas depende da conexão com o Telegram
- Se estiver escaneando um chat muito grande (>100k mensagens), considere rodar em background:
  ```bash
  nohup python3 scan_users.py > scan_output.log 2>&1 &
  ```

### "Session file not found"
- Se o script não encontrar a sessão automaticamente, crie um `.env` no diretório raiz
- Certifique-se de que a variável `SESSION_NAME` está definida

## Próximos Passos

Após identificar os maiores acervos:

1. **Priorizar processamento**: Comece pelos usuários com mais mídias
2. **Usar filtro**: Processar usuários específicos com `FILTER_USER`
3. **Criar lista JSON**: Gerar lista de usuários para processamento em lote
4. **Organizar por usuário**: Ativar `ORGANIZE_BY_USER` no clone_streaming.py

## Estrutura de Arquivos

Quando usa sessão existente:

```
dffdf/
├── scan_users.py              # Script de scan
├── scan.log                   # Log de execução (raiz)
└── sessions/
    └── session1/
        ├── .env              # Configurações da sessão
        ├── session1.session  # Sessão Telegram (já existe)
        ├── scan_media_by_user.json    # Resultados JSON
        ├── scan_report.txt           # Relatório formatado
        └── scan_checkpoint.txt       # Checkpoint (removido ao final)
```

## Suporte

Para issues ou sugestões, consulte o README principal do projeto.
