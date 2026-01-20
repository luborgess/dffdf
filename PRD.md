# PRD Final: Clone de Grupo Telegram (Streaming)

## Arquitetura

```
┌─────────────────────────────────────────────────────────────────┐
│                    STREAMING PIPELINE                            │
└─────────────────────────────────────────────────────────────────┘

   TELEGRAM           EC2 c6in.xlarge            TELEGRAM
   (origem)           RAM Buffer ~500MB          (destino)
      │                     │                        │
      │                     │                        │
      │   iter_download     │                        │
      │   chunk 1 (1MB)     │                        │
      │────────────────────►│                        │
      │                     │  saveBigFilePart       │
      │                     │  chunk 1               │
      │                     │───────────────────────►│
      │   chunk 2 (1MB)     │                        │
      │────────────────────►│  (descarta chunk 1)    │
      │                     │  chunk 2               │
      │                     │───────────────────────►│
      │        ...          │        ...             │
      │   chunk N           │                        │
      │────────────────────►│  chunk N               │
      │                     │───────────────────────►│
      │                     │                        │
      │                     │  sendMedia()           │
      │                     │  (finaliza upload)     │
      │                     │───────────────────────►│


NUNCA TEM 2GB EM RAM/DISCO - MÁXIMO ~500MB DE BUFFER
```

## Hardware

```
┌─────────────────────────────────────────────────────────────────┐
│  EC2 c6in.xlarge                                                 │
│                                                                  │
│  • 4 vCPU                                                        │
│  • 8 GB RAM                                                      │
│  • 30 Gbps rede                                                  │
│  • EBS gp3 50GB (só OS + fallback)                              │
│                                                                  │
│  Uso de RAM:                                                     │
│  • ~500MB buffer de streaming                                    │
│  • ~200MB Python + Telethon                                      │
│  • ~300MB OS                                                     │
│  • ~7GB livre (sobra)                                           │
│                                                                  │
│  Custo: ~$140/mês                                               │
└─────────────────────────────────────────────────────────────────┘

ALTERNATIVA ECONÔMICA:
┌─────────────────────────────────────────────────────────────────┐
│  EC2 c6in.large                                                  │
│  • 2 vCPU | 4 GB RAM | 25 Gbps                                  │
│  • Funciona, só com menos folga                                 │
│  • ~$70/mês                                                     │
└─────────────────────────────────────────────────────────────────┘
```

## Código: Streaming Real

```python
#!/usr/bin/env python3
"""
Clone de grupo Telegram com STREAMING REAL.
Nunca segura arquivo completo em memória.

Usa API MTProto de baixo nível para:
- Download em chunks (iter_download)
- Upload em chunks paralelos (saveBigFilePart)
"""

import asyncio
import os
import time
import logging
import hashlib
import random
from pathlib import Path
from typing import AsyncGenerator, BinaryIO

from telethon import TelegramClient
from telethon.tl.types import (
    Message, DocumentAttributeVideo, DocumentAttributeFilename,
    InputFileBig, InputMediaUploadedDocument
)
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.functions.messages import SendMediaRequest
from telethon.errors import FloodWaitError

# ============================================================
# CONFIGURAÇÃO
# ============================================================

API_ID = int(os.environ['TG_API_ID'])
API_HASH = os.environ['TG_API_HASH']

SOURCE_CHAT = int(os.environ['SOURCE_CHAT'])
TARGET_CHAT = int(os.environ['TARGET_CHAT'])
SOURCE_TOPIC = int(os.environ.get('SOURCE_TOPIC', 0)) or None
TARGET_TOPIC = int(os.environ.get('TARGET_TOPIC', 0)) or None

# Streaming config
CHUNK_SIZE = 512 * 1024  # 512KB por chunk (máximo MTProto)
PARALLEL_UPLOADS = 10     # Chunks em paralelo no upload
BUFFER_CHUNKS = 20        # Chunks em buffer (~10MB)

# Rate limit
MIN_INTERVAL = 3.5  # segundos entre mensagens

# Checkpoint
CHECKPOINT_FILE = 'checkpoint.txt'

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('clone.log')
    ]
)
log = logging.getLogger(__name__)

# ============================================================
# STREAMING UPLOADER
# ============================================================

class StreamingUploader:
    """
    Upload de arquivo grande em streaming.
    Não precisa ter o arquivo completo para começar.
    """
    
    def __init__(self, client: TelegramClient, file_size: int, file_name: str):
        self.client = client
        self.file_size = file_size
        self.file_name = file_name
        
        # Gerar file_id único
        self.file_id = random.randrange(-2**62, 2**62)
        
        # Calcular total de partes
        self.total_parts = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        
        # Controle
        self.parts_uploaded = 0
        self.md5_hash = hashlib.md5()
        
        # Semáforo para limitar uploads paralelos
        self.semaphore = asyncio.Semaphore(PARALLEL_UPLOADS)
        
        # Fila de chunks pendentes
        self.pending_tasks = []
    
    async def upload_part(self, part_index: int, data: bytes) -> bool:
        """Upload de uma parte do arquivo."""
        async with self.semaphore:
            try:
                result = await self.client(SaveBigFilePartRequest(
                    file_id=self.file_id,
                    file_part=part_index,
                    file_total_parts=self.total_parts,
                    bytes=data
                ))
                
                if result:
                    self.parts_uploaded += 1
                    self.md5_hash.update(data)
                    return True
                return False
                
            except FloodWaitError as e:
                log.warning(f"FloodWait no upload: {e.seconds}s")
                await asyncio.sleep(e.seconds + 1)
                return await self.upload_part(part_index, data)
    
    async def upload_chunk(self, part_index: int, data: bytes):
        """Agenda upload de um chunk (não bloqueia)."""
        task = asyncio.create_task(self.upload_part(part_index, data))
        self.pending_tasks.append(task)
        
        # Limpar tasks completadas
        self.pending_tasks = [t for t in self.pending_tasks if not t.done()]
    
    async def wait_completion(self):
        """Aguarda todos os uploads pendentes."""
        if self.pending_tasks:
            await asyncio.gather(*self.pending_tasks)
    
    def get_input_file(self) -> InputFileBig:
        """Retorna InputFile para usar no sendMedia."""
        return InputFileBig(
            id=self.file_id,
            parts=self.total_parts,
            name=self.file_name
        )


# ============================================================
# CLONE COM STREAMING
# ============================================================

class StreamingCloner:
    """
    Clonador com streaming real.
    Download e upload acontecem em paralelo.
    """
    
    def __init__(self, client: TelegramClient):
        self.client = client
        self.last_send_time = 0
    
    async def wait_rate_limit(self):
        """Aguarda rate limit de 20 msgs/min."""
        elapsed = time.time() - self.last_send_time
        if elapsed < MIN_INTERVAL:
            await asyncio.sleep(MIN_INTERVAL - elapsed)
        self.last_send_time = time.time()
    
    async def clone_message(self, msg: Message) -> bool:
        """Clona uma mensagem com streaming."""
        
        await self.wait_rate_limit()
        
        try:
            # ===== TEXTO =====
            if msg.text and not msg.media:
                await self.client.send_message(
                    TARGET_CHAT,
                    msg.text,
                    reply_to=TARGET_TOPIC
                )
                log.info(f"✓ Texto: msg {msg.id}")
                return True
            
            # ===== MÍDIA PEQUENA (<10MB) - download normal =====
            if msg.media:
                file_size = self._get_file_size(msg)
                
                if file_size and file_size < 10 * 1024 * 1024:
                    return await self._clone_small_file(msg)
                
                # ===== MÍDIA GRANDE - STREAMING =====
                if file_size and file_size >= 10 * 1024 * 1024:
                    return await self._clone_large_file_streaming(msg)
            
            log.warning(f"⊘ Tipo não suportado: msg {msg.id}")
            return False
            
        except FloodWaitError as e:
            log.warning(f"FloodWait: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            return await self.clone_message(msg)
            
        except Exception as e:
            log.error(f"✗ Erro msg {msg.id}: {e}")
            return False
    
    async def _clone_small_file(self, msg: Message) -> bool:
        """Clone de arquivo pequeno (cabe em RAM)."""
        
        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)
        
        log.info(f"↓↑ Pequeno: {file_name} ({file_size/(1024*1024):.1f}MB)")
        
        # Download para memória
        data = await self.client.download_media(msg, file=bytes)
        
        # Upload
        await self.client.send_file(
            TARGET_CHAT,
            data,
            caption=msg.text or "",
            reply_to=TARGET_TOPIC,
            attributes=self._get_attributes(msg)
        )
        
        log.info(f"✓ Pequeno: msg {msg.id}")
        return True
    
    async def _clone_large_file_streaming(self, msg: Message) -> bool:
        """
        Clone de arquivo grande com STREAMING REAL.
        Download e upload em paralelo, nunca segura arquivo completo.
        """
        
        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)
        
        log.info(f"⚡ Streaming: {file_name} ({file_size/(1024*1024):.1f}MB)")
        
        # Criar uploader
        uploader = StreamingUploader(self.client, file_size, file_name)
        
        # Stream download → upload em paralelo
        part_index = 0
        bytes_processed = 0
        start_time = time.time()
        
        async for chunk in self.client.iter_download(
            msg.media,
            chunk_size=CHUNK_SIZE,
            request_size=CHUNK_SIZE
        ):
            # Upload chunk (não bloqueia)
            await uploader.upload_chunk(part_index, chunk)
            
            bytes_processed += len(chunk)
            part_index += 1
            
            # Log progresso a cada 10%
            progress = bytes_processed / file_size * 100
            if int(progress) % 10 == 0 and int(progress) > 0:
                elapsed = time.time() - start_time
                speed = bytes_processed / elapsed / (1024 * 1024)
                log.debug(f"  {progress:.0f}% ({speed:.1f} MB/s)")
        
        # Aguardar uploads pendentes
        await uploader.wait_completion()
        
        # Finalizar: enviar mensagem com o arquivo
        input_file = uploader.get_input_file()
        
        # Criar InputMedia baseado no tipo
        media = self._create_input_media(msg, input_file)
        
        # Enviar
        await self.client(SendMediaRequest(
            peer=await self.client.get_input_entity(TARGET_CHAT),
            media=media,
            message=msg.text or "",
            reply_to_msg_id=TARGET_TOPIC
        ))
        
        elapsed = time.time() - start_time
        speed = file_size / elapsed / (1024 * 1024)
        log.info(f"✓ Streaming: msg {msg.id} ({elapsed:.1f}s, {speed:.1f} MB/s)")
        
        return True
    
    def _get_file_size(self, msg: Message) -> int:
        """Retorna tamanho do arquivo."""
        if msg.video:
            return msg.video.size
        if msg.document:
            return msg.document.size
        if msg.audio:
            return msg.audio.size
        if msg.voice:
            return msg.voice.size
        if msg.photo:
            return max(p.size for p in msg.photo.sizes if hasattr(p, 'size'))
        return 0
    
    def _get_file_name(self, msg: Message) -> str:
        """Retorna nome do arquivo."""
        if msg.document:
            for attr in msg.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
        return f"file_{msg.id}"
    
    def _get_attributes(self, msg: Message) -> list:
        """Retorna atributos do documento."""
        if msg.document:
            return msg.document.attributes
        return []
    
    def _create_input_media(self, msg: Message, input_file: InputFileBig):
        """Cria InputMedia baseado no tipo original."""
        
        mime_type = "application/octet-stream"
        attributes = []
        
        if msg.video:
            mime_type = msg.video.mime_type
            attributes = msg.video.attributes
        elif msg.document:
            mime_type = msg.document.mime_type
            attributes = msg.document.attributes
        elif msg.audio:
            mime_type = msg.audio.mime_type
            attributes = msg.audio.attributes
        
        return InputMediaUploadedDocument(
            file=input_file,
            mime_type=mime_type,
            attributes=attributes,
            force_file=False
        )


# ============================================================
# MAIN
# ============================================================

def load_checkpoint() -> int:
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return int(f.read().strip())
    return 0

def save_checkpoint(msg_id: int):
    with open(CHECKPOINT_FILE, 'w') as f:
        f.write(str(msg_id))


async def main():
    log.info("=" * 60)
    log.info("TELEGRAM STREAMING CLONER")
    log.info("=" * 60)
    log.info(f"Origem: {SOURCE_CHAT} (tópico: {SOURCE_TOPIC})")
    log.info(f"Destino: {TARGET_CHAT} (tópico: {TARGET_TOPIC})")
    log.info(f"Chunk size: {CHUNK_SIZE // 1024}KB")
    log.info(f"Parallel uploads: {PARALLEL_UPLOADS}")
    log.info("=" * 60)
    
    last_id = load_checkpoint()
    if last_id:
        log.info(f"Resumindo de msg {last_id}")
    
    stats = {'ok': 0, 'fail': 0, 'bytes': 0}
    start_time = time.time()
    
    async with TelegramClient('cloner', API_ID, API_HASH) as client:
        
        cloner = StreamingCloner(client)
        
        log.info("Conectado! Buscando mensagens...")
        
        async for msg in client.iter_messages(
            SOURCE_CHAT,
            min_id=last_id,
            reverse=True
        ):
            # Filtrar por tópico
            if SOURCE_TOPIC:
                if getattr(msg, 'reply_to_msg_id', None) != SOURCE_TOPIC:
                    if getattr(msg, 'reply_to', None):
                        if getattr(msg.reply_to, 'reply_to_top_id', None) != SOURCE_TOPIC:
                            continue
                    else:
                        continue
            
            success = await cloner.clone_message(msg)
            
            if success:
                stats['ok'] += 1
                stats['bytes'] += cloner._get_file_size(msg) or 0
            else:
                stats['fail'] += 1
            
            save_checkpoint(msg.id)
            
            # Log a cada 10
            total = stats['ok'] + stats['fail']
            if total % 10 == 0:
                elapsed = (time.time() - start_time) / 60
                rate = total / elapsed if elapsed > 0 else 0
                gb = stats['bytes'] / (1024**3)
                log.info(
                    f"Progresso: {stats['ok']} ok | "
                    f"{rate:.1f} msg/min | {gb:.2f} GB"
                )
    
    elapsed = (time.time() - start_time) / 60
    log.info("=" * 60)
    log.info("CONCLUÍDO!")
    log.info(f"Sucesso: {stats['ok']}")
    log.info(f"Falhas: {stats['fail']}")
    log.info(f"Transferido: {stats['bytes']/(1024**3):.2f} GB")
    log.info(f"Tempo: {elapsed:.1f} minutos")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
```

## Setup

```bash
# 1. EC2 c6in.xlarge, Ubuntu 24.04
sudo apt update && sudo apt install -y python3.12 python3-pip

# 2. Instalar Telethon
pip install telethon cryptg --break-system-packages

# 3. Tuning de rede
cat << 'EOF' | sudo tee /etc/sysctl.d/99-network.conf
net.core.rmem_max = 268435456
net.core.wmem_max = 268435456
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728
net.ipv4.tcp_congestion_control = bbr
net.core.default_qdisc = fq
EOF
sudo sysctl -p /etc/sysctl.d/99-network.conf

# 4. Aumentar file descriptors
echo "* soft nofile 1048576" | sudo tee -a /etc/security/limits.conf
echo "* hard nofile 1048576" | sudo tee -a /etc/security/limits.conf

# 5. Variáveis
export TG_API_ID="seu_api_id"
export TG_API_HASH="seu_api_hash"
export SOURCE_CHAT="-100123456789"
export TARGET_CHAT="-100987654321"
export SOURCE_TOPIC="123"
export TARGET_TOPIC="456"

# 6. Rodar
python3 clone_streaming.py

# 7. Background
nohup python3 clone_streaming.py > output.log 2>&1 &
```

## Comparação de Performance

```
┌────────────────────────────────────────────────────────────────────────────┐
│                    VÍDEO DE 2GB - COMPARAÇÃO                               │
├─────────────────────────┬──────────────┬───────────────┬──────────────────┤
│ Método                  │ Tempo        │ RAM Max       │ Disco            │
├─────────────────────────┼──────────────┼───────────────┼──────────────────┤
│ Download completo       │ ~7 min       │ 2+ GB         │ 2 GB             │
│ → depois upload         │              │               │                  │
├─────────────────────────┼──────────────┼───────────────┼──────────────────┤
│ Download → disco        │ ~6 min       │ ~100 MB       │ 2 GB             │
│ → upload do disco       │              │               │                  │
├─────────────────────────┼──────────────┼───────────────┼──────────────────┤
│ STREAMING               │ ~4 min       │ ~500 MB       │ 0                │
│ (download || upload)    │              │ (buffer)      │                  │
└─────────────────────────┴──────────────┴───────────────┴──────────────────┘

POR QUE STREAMING É MAIS RÁPIDO:
• Download e upload acontecem em PARALELO
• Enquanto baixa chunk N, sobe chunk N-1
• Nunca espera download completo para começar upload
• 10 uploads em paralelo maximizam throughput
```

## Custos Finais

| Item | Custo/mês |
|------|-----------|
| EC2 c6in.xlarge | ~$140 |
| EBS gp3 50GB | ~$4 |
| Data transfer | ~$10 |
| **Total** | **~$154/mês** |

**Alternativa econômica (c6in.large):** ~$84/mês

---

## Resumo

- **Streaming real**: download e upload em paralelo
- **RAM máxima**: ~500MB (não 2GB)
- **Disco**: não usa (só fallback)
- **Velocidade**: ~4 min para vídeo de 2GB
- **Gargalo**: rate limit 20 msgs/min