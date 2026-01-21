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
    InputFileBig, InputMediaUploadedDocument, InputReplyToMessage
)
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.functions.messages import SendMediaRequest
from telethon.errors import FloodWaitError

# Forum Topics - importar apenas se disponível
try:
    from telethon.tl.types import ForumTopic
    from telethon.tl.functions.channels import CreateForumTopicRequest, GetForumTopicsRequest
    FORUM_SUPPORT = True
except ImportError:
    FORUM_SUPPORT = False
    logging.warning("Forum Topics não suportado nesta versão do Telethon")

# ============================================================
# CONFIGURAÇÃO
# ============================================================

API_ID = int(os.environ['TG_API_ID'])
API_HASH = os.environ['TG_API_HASH']

SOURCE_CHAT = int(os.environ['SOURCE_CHAT'])
TARGET_CHAT = int(os.environ['TARGET_CHAT'])

# Helper para converter topic ID (trata string vazia)
def _parse_topic(val):
    if not val or val.strip() == '':
        return None
    return int(val)

SOURCE_TOPIC = _parse_topic(os.environ.get('SOURCE_TOPIC'))
TARGET_TOPIC = _parse_topic(os.environ.get('TARGET_TOPIC'))

# Auto-create topics in destination
AUTO_CREATE_TOPICS = os.environ.get('AUTO_CREATE_TOPICS', 'true').lower() == 'true'

# Topic mapping file (para persistência)
TOPIC_MAP_FILE = 'topic_map.json'

# Streaming config
CHUNK_SIZE = 512 * 1024  # 512KB por chunk (máximo MTProto)
PARALLEL_UPLOADS = 10     # Chunks em paralelo no upload
BUFFER_CHUNKS = 20        # Chunks em buffer (~10MB)

# Rate limit - Telegram permite ~30-50 msg/min
MIN_INTERVAL = 1.3  # ~46 msg/min (seguro dentro do limite)

# Checkpoint
CHECKPOINT_FILE = 'checkpoint.txt'

# Watermark
WATERMARK_PATH = os.path.expanduser('~/watermark.png')
WATERMARK_ENABLED = os.path.exists(WATERMARK_PATH)

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
# WATERMARK PROCESSOR
# ============================================================

import subprocess
from PIL import Image

def add_watermark_video(input_path: str, output_path: str) -> bool:
    """
    Adiciona watermark em vídeo usando FFmpeg.
    Posiciona em diagonal: superior esquerdo e inferior direito.
    """
    try:
        # Verificar tamanho do arquivo de entrada
        input_size = os.path.getsize(input_path)
        if input_size < 1000:
            log.warning(f"Arquivo de entrada muito pequeno: {input_size} bytes")
            return False

        # Filtro complexo para 2 watermarks em diagonal
        filter_complex = (
            '[1:v]scale=iw*0.225:-1,split=2[wm1][wm2];'
            '[0:v][wm1]overlay=10:10[tmp1];'
            '[tmp1][wm2]overlay=W-w-10:H-h-10'
        )
        cmd = [
            'ffmpeg', '-y',
            '-i', input_path,
            '-i', WATERMARK_PATH,
            '-filter_complex', filter_complex,
            '-c:v', 'libx264',
            '-c:a', 'copy',
            '-preset', 'ultrafast',
            '-crf', '23',
            '-threads', '0',
            '-movflags', '+faststart',
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=600)

        if result.returncode != 0:
            log.warning(f"FFmpeg erro: {result.stderr.decode()[-300:]}")
            return False

        # Verificar se arquivo de saída existe e tem tamanho razoável
        if not os.path.exists(output_path):
            log.warning("FFmpeg não criou arquivo de saída")
            return False

        output_size = os.path.getsize(output_path)
        if output_size < 1000:
            log.warning(f"Arquivo de saída muito pequeno: {output_size} bytes")
            os.remove(output_path)
            return False

        # Verificar se tamanho de saída é pelo menos 10% do original (não corrompido)
        if output_size < input_size * 0.1:
            log.warning(f"Arquivo de saída suspeito: {output_size} vs {input_size} bytes")
            os.remove(output_path)
            return False

        return True

    except subprocess.TimeoutExpired:
        log.error("FFmpeg timeout")
        return False
    except Exception as e:
        log.error(f"Erro watermark vídeo: {e}")
        return False


def add_watermark_image(input_path: str, output_path: str) -> bool:
    """
    Adiciona watermark em imagem usando Pillow.
    Posiciona em diagonal: superior esquerdo, centro e inferior direito.
    """
    try:
        base = Image.open(input_path).convert('RGBA')
        watermark = Image.open(WATERMARK_PATH).convert('RGBA')

        # Redimensionar watermark para 22.5% da largura da imagem (50% maior que 15%)
        wm_width = int(base.width * 0.225)
        wm_ratio = wm_width / watermark.width
        wm_height = int(watermark.height * wm_ratio)
        watermark = watermark.resize((wm_width, wm_height), Image.Resampling.LANCZOS)

        # Posições em diagonal (sem o centro)
        positions = [
            (10, 10),  # Superior esquerdo
            (base.width - wm_width - 10, base.height - wm_height - 10),  # Inferior direito
        ]

        # Aplicar watermark em cada posição
        for pos in positions:
            base.paste(watermark, pos, watermark)

        # Salvar
        if output_path.lower().endswith('.png'):
            base.save(output_path, 'PNG')
        else:
            base = base.convert('RGB')
            base.save(output_path, 'JPEG', quality=95)

        return True
    except Exception as e:
        log.error(f"Erro watermark imagem: {e}")
        return False


# ============================================================
# TOPIC MANAGER
# ============================================================

class TopicManager:
    """
    Gerencia criação automática de tópicos no destino.
    Mapeia tópicos da origem para tópicos criados no destino.
    """
    
    def __init__(self, client: TelegramClient):
        self.client = client
        self.topic_map: dict[int, int] = {}  # source_topic_id -> target_topic_id
        self.source_topics: dict[int, str] = {}  # topic_id -> topic_name
        self._load_map()
    
    def _load_map(self):
        """Carrega mapeamento de tópicos do arquivo."""
        import json
        if os.path.exists(TOPIC_MAP_FILE):
            try:
                with open(TOPIC_MAP_FILE, 'r') as f:
                    data = json.load(f)
                    self.topic_map = {int(k): int(v) for k, v in data.get('map', {}).items()}
                    self.source_topics = {int(k): v for k, v in data.get('names', {}).items()}
                log.info(f"📋 Carregado mapeamento de {len(self.topic_map)} tópicos")
            except Exception as e:
                log.warning(f"Erro ao carregar topic_map: {e}")
    
    def _save_map(self):
        """Salva mapeamento de tópicos no arquivo."""
        import json
        with open(TOPIC_MAP_FILE, 'w') as f:
            json.dump({
                'map': {str(k): v for k, v in self.topic_map.items()},
                'names': {str(k): v for k, v in self.source_topics.items()}
            }, f, indent=2)
    
    async def load_source_topics(self, source_chat: int):
        """Carrega informações dos tópicos do chat de origem."""
        if not FORUM_SUPPORT:
            log.warning("Forum Topics não suportado - atualize Telethon para versão mais recente")
            return
        
        try:
            result = await self.client(GetForumTopicsRequest(
                channel=source_chat,
                offset_date=0,
                offset_id=0,
                offset_topic=0,
                limit=100
            ))
            
            for topic in result.topics:
                if hasattr(topic, 'id') and hasattr(topic, 'title'):
                    self.source_topics[topic.id] = topic.title
            
            log.info(f"📚 Carregados {len(self.source_topics)} tópicos da origem")
        except Exception as e:
            log.warning(f"Não foi possível carregar tópicos da origem: {e}")
    
    def get_source_topic_id(self, msg: Message) -> int | None:
        """Extrai o ID do tópico de uma mensagem."""
        # Mensagem direta no tópico
        if hasattr(msg, 'reply_to') and msg.reply_to:
            # reply_to_top_id = ID do tópico
            if hasattr(msg.reply_to, 'reply_to_top_id') and msg.reply_to.reply_to_top_id:
                return msg.reply_to.reply_to_top_id
            # Se reply_to_msg_id existe e é um tópico root
            if hasattr(msg.reply_to, 'reply_to_msg_id') and msg.reply_to.reply_to_msg_id:
                if msg.reply_to.reply_to_msg_id in self.source_topics:
                    return msg.reply_to.reply_to_msg_id
        return None
    
    async def get_or_create_target_topic(self, source_topic_id: int, target_chat: int) -> int | None:
        """
        Retorna o tópico de destino correspondente.
        Cria automaticamente se não existir.
        """
        if not source_topic_id:
            return TARGET_TOPIC
        
        if not FORUM_SUPPORT:
            return TARGET_TOPIC
        
        # Já existe no mapa?
        if source_topic_id in self.topic_map:
            return self.topic_map[source_topic_id]
        
        # Criar tópico no destino
        topic_name = self.source_topics.get(source_topic_id, f"Tópico {source_topic_id}")
        
        try:
            log.info(f"📝 Criando tópico no destino: '{topic_name}'")
            
            result = await self.client(CreateForumTopicRequest(
                channel=target_chat,
                title=topic_name,
                icon_color=0x6FB9F0,  # Cor azul padrão
                random_id=random.randrange(-2**62, 2**62)
            ))
            
            # O ID do tópico é o ID da primeira mensagem (updates)
            new_topic_id = None
            if hasattr(result, 'updates'):
                for update in result.updates:
                    if hasattr(update, 'message') and hasattr(update.message, 'id'):
                        new_topic_id = update.message.id
                        break
            
            if new_topic_id:
                self.topic_map[source_topic_id] = new_topic_id
                self._save_map()
                log.info(f"✓ Tópico criado: '{topic_name}' (ID: {new_topic_id})")
                return new_topic_id
            else:
                log.error(f"Não foi possível obter ID do tópico criado")
                return TARGET_TOPIC
                
        except FloodWaitError as e:
            log.warning(f"FloodWait ao criar tópico: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            return await self.get_or_create_target_topic(source_topic_id, target_chat)
            
        except Exception as e:
            log.error(f"Erro ao criar tópico '{topic_name}': {e}")
            return TARGET_TOPIC

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
    Suporta criação automática de tópicos.
    """
    
    def __init__(self, client: TelegramClient, topic_manager: TopicManager = None):
        self.client = client
        self.topic_manager = topic_manager
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
        
        # Determinar tópico de destino
        target_topic = TARGET_TOPIC
        if AUTO_CREATE_TOPICS and self.topic_manager:
            source_topic_id = self.topic_manager.get_source_topic_id(msg)
            if source_topic_id:
                target_topic = await self.topic_manager.get_or_create_target_topic(
                    source_topic_id, TARGET_CHAT
                )
        
        try:
            # ===== TEXTO =====
            if msg.text and not msg.media:
                await self.client.send_message(
                    TARGET_CHAT,
                    msg.text,
                    reply_to=target_topic
                )
                log.info(f"✓ Texto: msg {msg.id}")
                return True
            
            # ===== MÍDIA PEQUENA (<10MB) - download normal =====
            if msg.media:
                file_size = self._get_file_size(msg)
                
                if file_size and file_size < 10 * 1024 * 1024:
                    return await self._clone_small_file(msg, target_topic)
                
                # ===== MÍDIA GRANDE - STREAMING =====
                if file_size and file_size >= 10 * 1024 * 1024:
                    return await self._clone_large_file_streaming(msg, target_topic)
            
            log.warning(f"⊘ Tipo não suportado: msg {msg.id}")
            return False
            
        except FloodWaitError as e:
            log.warning(f"FloodWait: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            return await self.clone_message(msg)
            
        except Exception as e:
            log.error(f"✗ Erro msg {msg.id}: {e}")
            return False
    
    async def _clone_small_file(self, msg: Message, target_topic: int = None) -> bool:
        """Clone de arquivo pequeno (cabe em RAM)."""
        import tempfile

        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)

        log.info(f"↓↑ Pequeno: {file_name} ({file_size/(1024*1024):.1f}MB)")

        # Download para arquivo temporário com nome correto
        tmp_dir = tempfile.gettempdir()
        tmp_path = os.path.join(tmp_dir, file_name)
        wm_path = os.path.join(tmp_dir, f"wm_{file_name}")

        try:
            await self.client.download_media(msg, file=tmp_path)

            # Detectar tipo de mídia
            is_video = msg.video is not None
            is_photo = msg.photo is not None
            supports_streaming = False
            upload_path = tmp_path  # Por padrão, enviar arquivo original

            # Aplicar watermark se habilitado
            if WATERMARK_ENABLED:
                if is_video:
                    log.info(f"🎬 Aplicando watermark em vídeo...")
                    if add_watermark_video(tmp_path, wm_path):
                        upload_path = wm_path
                        log.info(f"✓ Watermark aplicada")
                    else:
                        log.warning(f"⚠ Falha na watermark, enviando original")

                elif is_photo:
                    log.info(f"🖼 Aplicando watermark em foto...")
                    if add_watermark_image(tmp_path, wm_path):
                        upload_path = wm_path
                        log.info(f"✓ Watermark aplicada")
                    else:
                        log.warning(f"⚠ Falha na watermark, enviando original")

            # Preparar atributos e thumbnail para vídeos
            video_attrs = None
            thumb_path = None
            if is_video:
                # Extrair atributos do vídeo original
                for attr in (msg.video.attributes if msg.video else []):
                    if isinstance(attr, DocumentAttributeVideo):
                        supports_streaming = getattr(attr, 'supports_streaming', True)
                        video_attrs = attr
                        break

                # Gerar thumbnail do vídeo processado
                thumb_path = os.path.join(tmp_dir, f"thumb_{file_name}.jpg")
                try:
                    thumb_cmd = [
                        'ffmpeg', '-y',
                        '-i', upload_path,
                        '-ss', '00:00:01',
                        '-vframes', '1',
                        '-vf', 'scale=320:-1',
                        thumb_path
                    ]
                    subprocess.run(thumb_cmd, capture_output=True, timeout=30)
                except:
                    thumb_path = None

            await self.client.send_file(
                TARGET_CHAT,
                upload_path,
                caption=msg.text or "",
                reply_to=target_topic,
                force_document=False,
                supports_streaming=supports_streaming,
                thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
                attributes=[video_attrs] if video_attrs else None
            )

            # Limpar thumbnail
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)

            log.info(f"✓ Pequeno: msg {msg.id}")
            return True

        finally:
            # Limpar arquivos temporários
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if os.path.exists(wm_path):
                os.remove(wm_path)
    
    async def _clone_large_file_streaming(self, msg: Message, target_topic: int = None) -> bool:
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
        reply_to = InputReplyToMessage(reply_to_msg_id=target_topic) if target_topic else None
        await self.client(SendMediaRequest(
            peer=await self.client.get_input_entity(TARGET_CHAT),
            media=media,
            message=msg.text or "",
            reply_to=reply_to
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
        # Verificar documento
        if msg.document:
            for attr in msg.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
        # Verificar vídeo
        if msg.video:
            for attr in msg.video.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
            # Vídeos podem não ter filename, gerar com extensão correta
            ext = msg.video.mime_type.split('/')[-1] if msg.video.mime_type else 'mp4'
            return f"video_{msg.id}.{ext}"
        # Verificar áudio
        if msg.audio:
            for attr in msg.audio.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
            ext = msg.audio.mime_type.split('/')[-1] if msg.audio.mime_type else 'mp3'
            return f"audio_{msg.id}.{ext}"
        # Verificar foto
        if msg.photo:
            return f"photo_{msg.id}.jpg"
        return f"file_{msg.id}"
    
    def _get_attributes(self, msg: Message, override_filename: str = None) -> list:
        """Retorna atributos do documento, opcionalmente substituindo o filename."""
        attrs = []
        if msg.document:
            attrs = list(msg.document.attributes)
        elif msg.video:
            attrs = list(msg.video.attributes)
        elif msg.audio:
            attrs = list(msg.audio.attributes)

        # Se precisar sobrescrever o filename
        if override_filename:
            # Remove qualquer DocumentAttributeFilename existente
            attrs = [a for a in attrs if not isinstance(a, DocumentAttributeFilename)]
            # Adiciona o novo
            attrs.append(DocumentAttributeFilename(file_name=override_filename))

        return attrs
    
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
        
        # Inicializar Topic Manager
        topic_manager = None
        if AUTO_CREATE_TOPICS and FORUM_SUPPORT:
            log.info("Topic Manager: ATIVADO")
            topic_manager = TopicManager(client)
            await topic_manager.load_source_topics(SOURCE_CHAT)
        elif AUTO_CREATE_TOPICS and not FORUM_SUPPORT:
            log.warning("AUTO_CREATE_TOPICS configurado mas Forum não suportado - ignorando")
            
        cloner = StreamingCloner(client, topic_manager=topic_manager)
        
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
