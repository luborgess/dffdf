#!/usr/bin/env python3
"""
Clone de grupo Telegram organizado por usuário.

Processa usuários em ordem inteligente:
1. Mais mídias primeiro (máximo conteúdo rápido)
2. Menor tamanho primeiro (upload mais rápido)

Ignora usuários genéricos como "onlyfans" e "privacy" no primeiro batch.
"""

import asyncio
import os
import time
import logging
import json
import re
import tempfile
from pathlib import Path
from typing import Optional
from collections import defaultdict

from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.tl.types import (
    Message, DocumentAttributeVideo, DocumentAttributeFilename,
    InputFileBig, InputMediaUploadedDocument, InputReplyToMessage
)
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.functions.messages import SendMediaRequest, CreateForumTopicRequest, ForwardMessagesRequest
from telethon.errors import FloodWaitError

# ============================================================
# CONFIGURAÇÃO
# ============================================================

load_dotenv(Path(__file__).parent / '.env')

API_ID = int(os.environ['TG_API_ID'])
API_HASH = os.environ['TG_API_HASH']

SOURCE_CHAT = int(os.environ['SOURCE_CHAT'])
TARGET_CHAT = int(os.environ['TARGET_CHAT'])

# Arquivo de dados do scan
SCAN_DATA_FILE = 'scan_media_by_user.json'
USER_TOPIC_MAP_FILE = 'user_topic_map_clone.json'
CHECKPOINT_FILE = 'checkpoint_by_user.txt'

# Streaming config - BALANCEADO (evita FloodWait)
CHUNK_SIZE = 1024 * 1024  # 1MB
PARALLEL_UPLOADS = 20     # Upload paralelo de chunks
MIN_INTERVAL = 0.8        # ~75 msg/min (seguro)

# Batch processing
BATCH_CONCURRENT = 2      # Mensagens processadas em paralelo
USE_FORWARD = True        # Forward direto para arquivos sem watermark

# Watermark
WATERMARK_PATH = Path(__file__).parent / 'watermark.png'
WATERMARK_ENABLED = WATERMARK_PATH.exists()
WATERMARK_MAX_SIZE_MB = int(os.environ.get('WATERMARK_MAX_SIZE_MB', '50'))
WATERMARK_MAX_SIZE = WATERMARK_MAX_SIZE_MB * 1024 * 1024

# Usuários a IGNORAR no primeiro batch (muitos arquivos, genéricos)
IGNORE_USERS = {
    'onlyfans',    # 1825 mídias, 9.6 GB
    'privacy',     # 656 mídias, 3.3 GB
}

# Filtro: apenas usuários com no mínimo X mídias (acervos maiores)
MIN_MEDIA_PER_USER = int(os.environ.get('MIN_MEDIA_PER_USER', '10'))

# Limite máximo de usuários a processar (None = todos)
MAX_USERS = int(os.environ.get('MAX_USERS', '100'))

# Limite de mensagens por usuário (None = todas)
MAX_MSGS_PER_USER = os.environ.get('MAX_MSGS_PER_USER')
if MAX_MSGS_PER_USER:
    MAX_MSGS_PER_USER = int(MAX_MSGS_PER_USER)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('clone_by_user.log')
    ]
)
log = logging.getLogger(__name__)

# ============================================================
# WATERMARK
# ============================================================

import subprocess
from PIL import Image

# Tamanho FIXO da watermark em pixels (consistente em qualquer resolução)
WATERMARK_WIDTH_HORIZONTAL = 380  # Para vídeos 16:9, paisagem
WATERMARK_WIDTH_VERTICAL = 280    # Para vídeos 9:16, retrato
WATERMARK_MIN_WIDTH = 220         # Mínimo para vídeos muito pequenos
WATERMARK_MAX_WIDTH = 500         # Máximo para vídeos 4K+

def add_watermark_video(input_path: str, output_path: str) -> bool:
    """Adiciona watermark estática no canto superior esquerdo do vídeo."""
    try:
        input_size = os.path.getsize(input_path)
        if input_size < 1000:
            return False

        # Verificar se watermark existe e mostrar info
        wm_path_str = str(WATERMARK_PATH)
        if not os.path.exists(wm_path_str):
            log.error(f"  ❌ Watermark NÃO existe: {wm_path_str}")
            return False

        # Detectar dimensões do vídeo e calcular tamanho ideal da watermark
        wm_width = WATERMARK_WIDTH_HORIZONTAL  # Padrão
        video_width = 1920  # Fallback
        try:
            probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=width,height', '-of', 'csv=p=0', input_path]
            probe_result = subprocess.run(probe_cmd, capture_output=True, timeout=30)
            if probe_result.returncode == 0:
                dims = probe_result.stdout.decode().strip().split(',')
                if len(dims) == 2:
                    video_width, video_height = int(dims[0]), int(dims[1])
                    
                    # Escolher tamanho base conforme orientação
                    if video_height > video_width:
                        wm_width = WATERMARK_WIDTH_VERTICAL
                        orientation = "vertical"
                    else:
                        wm_width = WATERMARK_WIDTH_HORIZONTAL
                        orientation = "horizontal"
                    
                    # Ajustar proporcionalmente para resoluções muito diferentes de 1080p
                    # Referência: 1080p horizontal = 1920px, vertical = 1080px
                    ref_width = 1080 if video_height > video_width else 1920
                    scale_factor = video_width / ref_width
                    wm_width = int(wm_width * scale_factor)
                    
                    # Aplicar limites min/max
                    wm_width = max(WATERMARK_MIN_WIDTH, min(WATERMARK_MAX_WIDTH, wm_width))
                    
                    log.info(f"  📐 Vídeo {orientation} ({video_width}x{video_height}), watermark: {wm_width}px")
        except Exception:
            pass  # Usa tamanho padrão se falhar

        # Tamanho fixo em pixels, canto superior esquerdo, estática
        filter_complex = (
            f'[1:v]scale={wm_width}:-1[wm];'
            '[wm]format=yuva420p,colorchannelmixer=aa=0.5[wm_alpha];'
            '[0:v][wm_alpha]overlay=10:10'
        )

        cmd = [
            'ffmpeg', '-y', '-i', input_path, '-i', wm_path_str,
            '-filter_complex', filter_complex,
            '-c:v', 'libx264', '-c:a', 'copy', '-preset', 'veryfast',
            '-crf', '26', '-threads', '0', '-movflags', '+faststart',
            '-tune', 'fastdecode',
            output_path
        ]
        log.debug(f"  🎬 Comando ffmpeg: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True, timeout=600)

        if result.returncode != 0:
            log.error(f"  ❌ FFmpeg erro (code {result.returncode}):")
            log.error(f"     stderr: {result.stderr.decode('utf-8', errors='ignore')[-500:]}")
            return False

        if not os.path.exists(output_path):
            log.error(f"  ❌ Output não criado: {output_path}")
            return False

        output_size = os.path.getsize(output_path)
        if output_size < input_size * 0.1:
            log.error(f"  ❌ Output muito pequeno: {output_size} < {input_size * 0.1}")
            os.remove(output_path)
            return False

        log.info(f"  ✅ Watermark aplicada: {output_size/(1024**2):.1f}MB")
        return True
    except Exception as e:
        log.error(f"  ❌ Exceção watermark: {e}")
        return False


def add_watermark_image(input_path: str, output_path: str) -> bool:
    """Adiciona watermark no canto superior esquerdo da imagem."""
    try:
        base = Image.open(input_path).convert('RGBA')
        watermark = Image.open(WATERMARK_PATH).convert('RGBA')

        img_width, img_height = base.size
        
        # Escolher tamanho base conforme orientação
        if img_height > img_width:
            # Imagem vertical (retrato)
            wm_width = WATERMARK_WIDTH_VERTICAL
            orientation = "vertical"
            ref_width = 1080
        else:
            # Imagem horizontal (paisagem)
            wm_width = WATERMARK_WIDTH_HORIZONTAL
            orientation = "horizontal"
            ref_width = 1920
        
        # Ajustar proporcionalmente à resolução (referência: 1080p)
        scale_factor = img_width / ref_width
        wm_width = int(wm_width * scale_factor)
        
        # Aplicar limites min/max
        wm_width = max(WATERMARK_MIN_WIDTH, min(WATERMARK_MAX_WIDTH, wm_width))
        
        log.debug(f"  📐 Imagem {orientation} ({img_width}x{img_height}), watermark: {wm_width}px")
        
        # Redimensionar watermark mantendo proporção
        wm_ratio = wm_width / watermark.width
        wm_height = int(watermark.height * wm_ratio)
        watermark = watermark.resize((wm_width, wm_height), Image.Resampling.LANCZOS)

        watermark_alpha = watermark.split()[3]
        watermark_alpha = watermark_alpha.point(lambda p: int(p * 0.5))
        watermark.putalpha(watermark_alpha)

        # Canto superior esquerdo com margem de 10px
        pos = (10, 10)
        base.paste(watermark, pos, watermark)

        if output_path.lower().endswith('.png'):
            base.save(output_path, 'PNG')
        else:
            base = base.convert('RGB')
            base.save(output_path, 'JPEG', quality=95)

        return True
    except Exception:
        return False


def generate_video_thumbnail(video_path: str, thumb_path: str, is_preview: bool = False) -> bool:
    """Gera thumbnail de vídeo."""
    time_points = ['0', '0.5', '1', '2', '3', '5', '10']

    for time_point in time_points:
        try:
            if is_preview:
                cmd = ['ffmpeg', '-y', '-i', video_path, '-ss', time_point,
                       '-vframes', '1', '-vf', 'scale=320:-1', '-q:v', '2', thumb_path]
            else:
                cmd = ['ffmpeg', '-y', '-ss', time_point, '-i', video_path,
                       '-vframes', '1', '-vf', 'scale=320:-1', '-q:v', '2', thumb_path]

            result = subprocess.run(cmd, capture_output=True, timeout=60)

            if result.returncode == 0 and os.path.exists(thumb_path):
                if os.path.getsize(thumb_path) > 100:
                    return True
                else:
                    os.remove(thumb_path)
        except Exception:
            continue

    return False


# ============================================================
# USER DATA LOADER
# ============================================================

def load_scan_data() -> dict:
    """Carrega dados do scan e ordena usuários."""
    if not os.path.exists(SCAN_DATA_FILE):
        log.error(f"Arquivo de scan não encontrado: {SCAN_DATA_FILE}")
        log.error("Execute scan_users.py primeiro!")
        raise FileNotFoundError(SCAN_DATA_FILE)

    with open(SCAN_DATA_FILE, 'r') as f:
        data = json.load(f)

    users = data.get('users', {})

    # Converter para lista e filtrar
    user_list = []
    skipped_small = 0
    skipped_ignored = 0
    skipped_no_ids = 0

    for username, stats in users.items():
        if username in IGNORE_USERS:
            skipped_ignored += 1
            continue

        media_count = stats['total_media']
        message_ids = stats.get('message_ids', [])

        # Verificar se tem message_ids
        if not message_ids:
            skipped_no_ids += 1
            continue

        # Filtro: apenas usuários com PELO MENOS X mídias (acervos maiores)
        if media_count < MIN_MEDIA_PER_USER:
            skipped_small += 1
            continue

        if media_count == 0:
            continue

        user_list.append({
            'username': username,
            'total_media': stats['total_media'],
            'total_bytes': stats['total_bytes'],
            'videos': stats.get('videos', 0),
            'photos': stats.get('photos', 0),
            'albums': stats.get('albums', 0),
            'message_ids': message_ids,
            'album_grouped_ids': stats.get('album_grouped_ids', []),
        })

    log.info(f"📊 Filtro de usuários:")
    log.info(f"   Ignorados (lista): {skipped_ignored}")
    log.info(f"   Pulados (< {MIN_MEDIA_PER_USER} mídias): {skipped_small}")
    log.info(f"   Pulados (sem message_ids): {skipped_no_ids}")
    log.info(f"   Selecionados (>= {MIN_MEDIA_PER_USER} mídias): {len(user_list)}")

    # Ordenar: mais mídias primeiro, MENOR tamanho primeiro (para progresso rápido)
    user_list.sort(key=lambda u: (-u['total_media'], u['total_bytes']))

    # Aplicar limite
    if MAX_USERS and len(user_list) > MAX_USERS:
        user_list = user_list[:MAX_USERS]

    return user_list


# ============================================================
# USERNAME EXTRACTOR
# ============================================================

class UsernameExtractor:
    """Extrai usernames de legendas."""

    USERNAME_PATTERNS = [
        r'[⭐★]\s*»\s*([A-Za-z0-9_.-]+)',
        r'@([A-Za-z0-9_]+)',
        r'^([A-Za-z0-9_.-]+)\s*[-|]\s*(?:Onlyfans|OF)',
        r'(?:Onlyfans|OF)[\s:]+([A-Za-z0-9_.-]+)',
        r'^([A-Za-z0-9_.-]{3,30})(?:\s*[\n🔥❤️💦]|$)',
    ]

    def extract_username(self, caption: str) -> Optional[str]:
        if not caption:
            return None

        for pattern in self.USERNAME_PATTERNS:
            match = re.search(pattern, caption, re.IGNORECASE | re.MULTILINE)
            if match:
                username = match.group(1).strip().lower()
                if len(username) >= 2:
                    return username
        return None

    def extract_username_from_album(self, messages: list) -> Optional[str]:
        """Extrai username de um álbum (busca na mensagem com legenda maior)."""
        best_caption = ""
        for msg in messages:
            caption = getattr(msg, 'text', '') or getattr(msg, 'message', '') or ''
            if len(caption) > len(best_caption):
                best_caption = caption

        return self.extract_username(best_caption)


# ============================================================
# USER TOPIC MANAGER
# ============================================================

class UserTopicManager:
    """Gerencia tópicos por usuário no destino."""

    def __init__(self, client: TelegramClient):
        self.client = client
        self.user_map: dict[str, int] = {}
        self._load_map()

    def _load_map(self):
        if os.path.exists(USER_TOPIC_MAP_FILE):
            try:
                with open(USER_TOPIC_MAP_FILE, 'r') as f:
                    data = json.load(f)
                    self.user_map = data.get('users', {})
                log.info(f"📋 Carregados {len(self.user_map)} tópicos de usuários")
            except Exception as e:
                log.warning(f"Erro ao carregar user_topic_map: {e}")

    def _save_map(self):
        with open(USER_TOPIC_MAP_FILE, 'w') as f:
            json.dump({'users': self.user_map}, f, indent=2, ensure_ascii=False)

    async def get_or_create_user_topic(self, username: str, target_chat: int) -> Optional[int]:
        """Retorna o tópico para um usuário, criando se necessário."""
        # Se já tem mapeado, verificar se ainda existe
        if username in self.user_map:
            cached_id = self.user_map[username]
            # O tópico pode ter sido deletado - tentaremos usar,
            # se falhar será recriado no próximo uso
            return cached_id

        topic_name = f"🔥 {username}"

        try:
            log.info(f"  ➕ Criando tópico: '{topic_name}'")

            result = await self.client(CreateForumTopicRequest(
                peer=target_chat,
                title=topic_name,
                icon_color=0xFFD67E,
                random_id=-1  # placeholder
            ))

            new_topic_id = None
            if hasattr(result, 'updates'):
                for update in result.updates:
                    if hasattr(update, 'message') and hasattr(update.message, 'id'):
                        new_topic_id = update.message.id
                        break

            if new_topic_id:
                self.user_map[username] = new_topic_id
                self._save_map()
                log.info(f"  ✓ Tópico criado: ID {new_topic_id}")
                return new_topic_id

        except FloodWaitError as e:
            log.warning(f"  ⏳ FloodWait: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            return await self.get_or_create_user_topic(username, target_chat)

        except Exception as e:
            log.error(f"  ✗ Erro ao criar tópico: {e}")

        return None

    def invalidate_topic(self, username: str):
        """Remove um tópico do mapa (para forçar recriação)."""
        if username in self.user_map:
            del self.user_map[username]
            self._save_map()
            log.warning(f"  🗑 Tópico de '{username}' removido do mapa")

    def reset_all(self):
        """Remove todos os tópicos do mapa (força recriação de tudo)."""
        count = len(self.user_map)
        self.user_map.clear()
        self._save_map()
        log.warning(f"  🗑 {count} tópicos removidos do mapa")


# ============================================================
# STREAMING UPLOADER
# ============================================================

class StreamingUploader:
    """Upload de arquivo grande em streaming."""

    def __init__(self, client: TelegramClient, file_size: int, file_name: str):
        self.client = client
        self.file_size = file_size
        self.file_name = file_name
        self.file_id = -1  # será gerado
        self.total_parts = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        self.parts_uploaded = 0
        self.semaphore = asyncio.Semaphore(PARALLEL_UPLOADS)
        self.pending_tasks = []

    async def upload_part(self, part_index: int, data: bytes) -> bool:
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
                    return True
                return False
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
                return await self.upload_part(part_index, data)

    async def upload_chunk(self, part_index: int, data: bytes):
        task = asyncio.create_task(self.upload_part(part_index, data))
        self.pending_tasks.append(task)
        self.pending_tasks = [t for t in self.pending_tasks if not t.done()]

    async def wait_completion(self):
        if self.pending_tasks:
            await asyncio.gather(*self.pending_tasks)

    def get_input_file(self) -> InputFileBig:
        return InputFileBig(
            id=self.file_id,
            parts=self.total_parts,
            name=self.file_name
        )


# ============================================================
# USER CLONER
# ============================================================

class UserCloner:
    """Clona mídias de um usuário específico."""

    def __init__(self, client: TelegramClient, username: str,
                 topic_manager: UserTopicManager, stats: dict):
        self.client = client
        self.username = username
        self.topic_manager = topic_manager
        self.stats = stats  # {uploaded, failed, bytes}
        self.last_send_time = 0
        self.extractor = UsernameExtractor()

    async def wait_rate_limit(self):
        elapsed = time.time() - self.last_send_time
        if elapsed < MIN_INTERVAL:
            await asyncio.sleep(MIN_INTERVAL - elapsed)
        self.last_send_time = time.time()

    def _get_file_size(self, msg: Message) -> int:
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
        if msg.document:
            for attr in msg.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
        if msg.video:
            for attr in msg.video.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
            ext = msg.video.mime_type.split('/')[-1] if msg.video.mime_type else 'mp4'
            return f"video_{msg.id}.{ext}"
        if msg.audio:
            for attr in msg.audio.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
            ext = msg.audio.mime_type.split('/')[-1] if msg.audio.mime_type else 'mp3'
            return f"audio_{msg.id}.{ext}"
        if msg.photo:
            return f"photo_{msg.id}.jpg"
        return f"file_{msg.id}"

    async def clone_message(self, msg: Message, target_topic: int) -> bool:
        """Clona uma mensagem individual."""
        await self.wait_rate_limit()

        try:
            if msg.text and not msg.media:
                await self.client.send_message(
                    TARGET_CHAT,
                    msg.text,
                    reply_to=target_topic
                )
                return True

            if msg.media:
                file_size = self._get_file_size(msg)
                is_video = msg.video is not None
                is_photo = msg.photo is not None
                
                # Decidir se precisa de watermark
                needs_watermark = (
                    WATERMARK_ENABLED and
                    (is_video or is_photo) and
                    (WATERMARK_MAX_SIZE_MB == 0 or file_size <= WATERMARK_MAX_SIZE)
                )

                # OTIMIZAÇÃO: Forward direto se não precisa de watermark
                if USE_FORWARD and not needs_watermark:
                    return await self._forward_message(msg, target_topic)

                if file_size and file_size < 10 * 1024 * 1024:
                    return await self._clone_small_file(msg, target_topic)
                else:
                    if needs_watermark and is_video:
                        return await self._clone_large_video_with_watermark(msg, target_topic)
                    else:
                        return await self._clone_large_file_streaming(msg, target_topic)

            return False

        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            return await self.clone_message(msg, target_topic)

        except Exception as e:
            error_str = str(e).lower()
            # Detectar erro de tópico inválido/deletado
            if any(x in error_str for x in ['topic', 'forum', 'invalid', 'not found']):
                log.warning(f"  ⚠️ Erro de tópico para {self.username}, invalidando...")
                self.topic_manager.invalidate_topic(self.username)
            log.debug(f"  ✗ Erro msg {msg.id}: {e}")
            return False

    async def _forward_message(self, msg: Message, target_topic: int) -> bool:
        """Forward direto - MUITO mais rápido que download+upload."""
        try:
            await self.client(ForwardMessagesRequest(
                from_peer=SOURCE_CHAT,
                id=[msg.id],
                to_peer=TARGET_CHAT,
                top_msg_id=target_topic,
                drop_author=True,
                drop_media_captions=True
            ))
            return True
        except Exception as e:
            log.debug(f"  Forward falhou, tentando clone: {e}")
            # Fallback para clone tradicional
            return await self._clone_small_file(msg, target_topic) if self._get_file_size(msg) < 10*1024*1024 else await self._clone_large_file_streaming(msg, target_topic)

    async def _clone_small_file(self, msg: Message, target_topic: int) -> bool:
        """Clone de arquivo pequeno."""
        import tempfile

        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)

        tmp_dir = tempfile.gettempdir()
        tmp_path = os.path.join(tmp_dir, file_name)
        wm_path = os.path.join(tmp_dir, f"wm_{file_name}")

        try:
            await self.client.download_media(msg, file=tmp_path)

            is_video = msg.video is not None
            is_photo = msg.photo is not None
            upload_path = tmp_path

            if WATERMARK_ENABLED:
                if is_video and add_watermark_video(tmp_path, wm_path):
                    upload_path = wm_path
                elif is_photo and add_watermark_image(tmp_path, wm_path):
                    upload_path = wm_path

            # Telegram gera thumbnail automaticamente para arquivos pequenos
            video_attrs = None
            supports_streaming = False

            if is_video:
                for attr in (msg.video.attributes if msg.video else []):
                    if isinstance(attr, DocumentAttributeVideo):
                        supports_streaming = getattr(attr, 'supports_streaming', True)
                        video_attrs = attr
                        break

            await self.client.send_file(
                TARGET_CHAT,
                upload_path,
                caption='',
                reply_to=target_topic,
                force_document=False,
                supports_streaming=supports_streaming,
                attributes=[video_attrs] if video_attrs else None
            )

            return True

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if os.path.exists(wm_path):
                os.remove(wm_path)

    async def _clone_large_video_with_watermark(self, msg: Message, target_topic: int) -> bool:
        """Clone de vídeo grande COM watermark."""
        import tempfile

        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)

        tmp_dir = tempfile.gettempdir()
        tmp_path = os.path.join(tmp_dir, file_name)
        wm_path = os.path.join(tmp_dir, f"wm_{file_name}")
        thumb_path = os.path.join(tmp_dir, f"thumb_{file_name}.jpg")

        try:
            await self.client.download_media(msg, file=tmp_path)

            upload_path = tmp_path
            if add_watermark_video(tmp_path, wm_path):
                upload_path = wm_path

            thumb_generated = generate_video_thumbnail(upload_path, thumb_path, is_preview=False)

            supports_streaming = True
            video_attrs = None
            for attr in (msg.video.attributes if msg.video else []):
                if isinstance(attr, DocumentAttributeVideo):
                    supports_streaming = getattr(attr, 'supports_streaming', True)
                    video_attrs = attr
                    break

            await self.client.send_file(
                TARGET_CHAT,
                upload_path,
                caption='',
                reply_to=target_topic,
                force_document=False,
                supports_streaming=supports_streaming,
                thumb=thumb_path if thumb_generated else None,
                attributes=[video_attrs] if video_attrs else None
            )

            return True

        finally:
            for path in [tmp_path, wm_path, thumb_path]:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except:
                        pass

    async def _clone_large_file_streaming(self, msg: Message, target_topic: int) -> bool:
        """Clone de arquivo grande com streaming."""
        import tempfile
        import random

        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)
        is_video = msg.video is not None

        uploader = StreamingUploader(self.client, file_size, file_name)
        uploader.file_id = random.randrange(-2**62, 2**62)

        tmp_dir = tempfile.gettempdir()
        video_preview_path = os.path.join(tmp_dir, f"preview_{file_name}") if is_video else None
        thumb_path = os.path.join(tmp_dir, f"thumb_{file_name}.jpg") if is_video else None
        preview_bytes = 0
        preview_file = None
        thumb_generated = False
        PREVIEW_SIZE = 10 * 1024 * 1024

        if is_video:
            preview_file = open(video_preview_path, 'wb')

        part_index = 0
        bytes_processed = 0
        start_time = time.time()

        try:
            async for chunk in self.client.iter_download(
                msg.media,
                chunk_size=CHUNK_SIZE,
                request_size=CHUNK_SIZE
            ):
                await uploader.upload_chunk(part_index, chunk)

                if is_video and preview_file and preview_bytes < PREVIEW_SIZE:
                    preview_file.write(chunk)
                    preview_bytes += len(chunk)

                    if preview_bytes >= PREVIEW_SIZE and not thumb_generated:
                        preview_file.close()
                        preview_file = None
                        thumb_generated = generate_video_thumbnail(video_preview_path, thumb_path, is_preview=True)

                bytes_processed += len(chunk)
                part_index += 1

            if preview_file:
                preview_file.close()
                preview_file = None

            await uploader.wait_completion()

            thumb_input_file = None
            if thumb_generated and thumb_path and os.path.exists(thumb_path):
                try:
                    thumb_input_file = await self.client.upload_file(thumb_path)
                except:
                    pass

            input_file = uploader.get_input_file()

            # Criar InputMedia
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

            media = InputMediaUploadedDocument(
                file=input_file,
                mime_type=mime_type,
                attributes=attributes,
                thumb=thumb_input_file,
                force_file=False
            )

            reply_to = InputReplyToMessage(reply_to_msg_id=target_topic) if target_topic else None
            await self.client(SendMediaRequest(
                peer=await self.client.get_input_entity(TARGET_CHAT),
                media=media,
                message="",
                reply_to=reply_to
            ))

            return True

        finally:
            if preview_file:
                preview_file.close()
            if video_preview_path and os.path.exists(video_preview_path):
                os.remove(video_preview_path)
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)


# ============================================================
# USER BATCH CLONER
# ============================================================

class UserBatchCloner:
    """Processa múltiplos usuários em sequência."""

    def __init__(self, client: TelegramClient, users: list, topic_manager: UserTopicManager):
        self.client = client
        self.users = users
        self.topic_manager = topic_manager
        self.checkpoint_data = self._load_checkpoint()

    def _load_checkpoint(self) -> dict:
        """Carrega checkpoint com usuário e índice de mensagem."""
        if os.path.exists(CHECKPOINT_FILE):
            try:
                with open(CHECKPOINT_FILE, 'r') as f:
                    data = f.read().strip()
                    if ':' in data:
                        username, msg_index = data.split(':', 1)
                        return {'username': username, 'msg_index': int(msg_index)}
            except:
                pass
        return {}

    def _save_checkpoint(self, username: str, msg_index: int):
        """Salva checkpoint com usuário e índice da última mensagem processada."""
        with open(CHECKPOINT_FILE, 'w') as f:
            f.write(f"{username}:{msg_index}")

    def _clear_checkpoint(self):
        if os.path.exists(CHECKPOINT_FILE):
            os.remove(CHECKPOINT_FILE)

    async def process_user(self, user_data: dict, start_msg_index: int = 0) -> dict:
        """Processa todas as mídias de um usuário usando os message_ids do scan."""
        username = user_data['username']
        expected_media = user_data['total_media']
        all_message_ids = user_data.get('message_ids', [])
        
        # Filtrar mensagens já processadas (retomada de checkpoint)
        if start_msg_index > 0:
            message_ids = all_message_ids[start_msg_index:]
            log.info(f"")
            log.info(f"{'='*60}")
            log.info(f"👤 USUÁRIO: {username} (RETOMANDO)")
            log.info(f"   ⏭ Pulando {start_msg_index} mensagens já processadas")
            log.info(f"   Restantes: {len(message_ids)} de {len(all_message_ids)}")
        else:
            message_ids = all_message_ids
            log.info(f"")
            log.info(f"{'='*60}")
            log.info(f"👤 USUÁRIO: {username}")
        
        log.info(f"   Expected: {expected_media} mídias | {user_data['videos']} vídeos | {user_data['photos']} fotos")
        log.info(f"   Tamanho: {user_data['total_bytes']/(1024**2):.1f} MB")
        log.info(f"   Message IDs: {len(message_ids)}")
        log.info(f"{'='*60}")

        # Criar tópico
        target_topic = await self.topic_manager.get_or_create_user_topic(username, TARGET_CHAT)
        if not target_topic:
            log.error(f"Não foi possível criar tópico para {username}")
            return {'uploaded': 0, 'failed': 0, 'bytes': 0}

        # Inicializar cloner e stats
        stats = {'uploaded': 0, 'failed': 0, 'bytes': 0}
        cloner = UserCloner(self.client, username, self.topic_manager, stats)

        # Buscar mensagens pelos IDs do scan
        start_time = time.time()
        processed_count = 0
        
        # Índice base para checkpoint (considera mensagens já puladas)
        base_index = start_msg_index

        # Buscar mensagens em lote (Telethon suporta buscar por lista de IDs)
        # OTIMIZADO: Processamento em batch paralelo
        batch_size = 100
        semaphore = asyncio.Semaphore(BATCH_CONCURRENT)
        
        async def process_single(msg):
            """Processa uma mensagem com controle de concorrência."""
            async with semaphore:
                success = await cloner.clone_message(msg, target_topic)
                return (msg, success)
        
        for i in range(0, len(message_ids), batch_size):
            batch_ids = message_ids[i:i + batch_size]
            
            # Coletar mensagens do batch
            messages = []
            async for msg in self.client.iter_messages(SOURCE_CHAT, ids=batch_ids):
                if msg:
                    messages.append(msg)
            
            if MAX_MSGS_PER_USER and stats['uploaded'] >= MAX_MSGS_PER_USER:
                break
            
            # Processar em paralelo (BATCH_CONCURRENT ao mesmo tempo)
            tasks = [process_single(msg) for msg in messages]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for result in results:
                if isinstance(result, Exception):
                    stats['failed'] += 1
                    continue
                msg, success = result
                if success:
                    stats['uploaded'] += 1
                    stats['bytes'] += cloner._get_file_size(msg) or 0
                else:
                    stats['failed'] += 1
                processed_count += 1
            
            # Salvar checkpoint com índice absoluto (base + processados neste batch)
            current_index = base_index + i + len(messages)
            self._save_checkpoint(username, current_index)
            
            # Progresso a cada 10 mensagens
            if processed_count % 10 == 0:
                self._print_progress(username, stats, expected_media, start_time)

            if MAX_MSGS_PER_USER and stats['uploaded'] >= MAX_MSGS_PER_USER:
                log.info(f"  ⏭ Limite de {MAX_MSGS_PER_USER} mensagens atingido")
                break

        elapsed = time.time() - start_time
        log.info(f"")
        log.info(f"  ✓ {username} completo:")
        log.info(f"    Upload: {stats['uploaded']} mídias")
        log.info(f"    Bytes: {stats['bytes']/(1024**2):.1f} MB")
        log.info(f"    Tempo: {elapsed:.1f}s")
        if stats['failed'] > 0:
            log.info(f"    Falhas: {stats['failed']}")

        self._clear_checkpoint()
        return stats

    def _print_progress(self, username: str, stats: dict, expected: int, start_time: float):
        """Imprime progresso do usuário."""
        uploaded = stats['uploaded']
        elapsed = time.time() - start_time
        rate = uploaded / elapsed if elapsed > 0 else 0
        pct = (uploaded / expected * 100) if expected > 0 else 0

        log.info(f"  📊 {username}: {uploaded}/{expected} ({pct:.0f}%) | {rate:.1f} msg/s")

    async def process_all(self) -> dict:
        """Processa todos os usuários na ordem."""
        total_stats = {'uploaded': 0, 'failed': 0, 'bytes': 0, 'users': 0}
        start_time = time.time()

        # Encontrar onde retomar
        start_user_index = 0
        start_msg_index = 0

        if self.checkpoint_data:
            checkpoint_user = self.checkpoint_data.get('username')
            if checkpoint_user:
                for i, user in enumerate(self.users):
                    if user['username'] == checkpoint_user:
                        start_user_index = i
                        start_msg_index = self.checkpoint_data.get('msg_index', 0)
                        total_msgs = len(user.get('message_ids', []))
                        log.info(f"📍 Retomando do usuário '{checkpoint_user}'")
                        log.info(f"   ⏭ Já processadas: {start_msg_index}/{total_msgs} mensagens")
                        break

        # Processar cada usuário
        for i in range(start_user_index, len(self.users)):
            user = self.users[i]
            user_num = i + 1

            log.info(f"")
            log.info(f"🔄 [{user_num}/{len(self.users)}] Processando: {user['username']}")
            log.info(f"   📊 Fila restante: {len(self.users) - user_num} usuários")

            stats = await self.process_user(user, start_msg_index)

            total_stats['uploaded'] += stats['uploaded']
            total_stats['failed'] += stats['failed']
            total_stats['bytes'] += stats['bytes']
            total_stats['users'] += 1

            # Resetar para próximo usuário (começa do zero)
            start_msg_index = 0

            # Progresso geral
            elapsed_total = time.time() - start_time
            log.info(f"")
            log.info(f"📈 PROGRESSO GERAL:")
            log.info(f"   Usuários: {total_stats['users']}/{len(self.users)}")
            log.info(f"   Mídias: {total_stats['uploaded']} uploadadas")
            log.info(f"   Bytes: {total_stats['bytes']/(1024**3):.2f} GB")
            log.info(f"   Tempo: {elapsed_total/60:.1f} min")

        return total_stats


# ============================================================
# MAIN
# ============================================================

async def main():
    log.info("=" * 60)
    log.info("CLONE POR USUÁRIO - ORDEM INTELIGENTE")
    log.info("=" * 60)
    log.info(f"Origem: {SOURCE_CHAT}")
    log.info(f"Destino: {TARGET_CHAT}")
    log.info(f"Filtro: usuários com >= {MIN_MEDIA_PER_USER} mídias")
    log.info(f"Máx usuários: {MAX_USERS if MAX_USERS else 'todos'}")
    log.info(f"Ignorar: {', '.join(IGNORE_USERS)}")
    log.info("=" * 60)

    # Carregar e ordenar usuários
    users = load_scan_data()

    log.info(f"")
    log.info(f"📋 {len(users)} usuários para processar:")
    log.info("")

    # Mostrar top 20
    for i, user in enumerate(users[:20], 1):
        size_mb = user['total_bytes'] / (1024 * 1024)
        size_str = f"{size_mb:.1f}MB" if size_mb < 1024 else f"{size_mb/1024:.2f}GB"
        log.info(f"  {i:2d}. {user['username']:20s} - {user['total_media']:4d} mídias | {size_str}")

    if len(users) > 20:
        log.info(f"  ... e {len(users) - 20} mais")

    # Estimativas
    total_media = sum(u['total_media'] for u in users)
    total_bytes = sum(u['total_bytes'] for u in users)
    log.info(f"")
    log.info(f"📊 ESTIMATIVA:")
    log.info(f"   Total mídias: {total_media:,}")
    log.info(f"   Total bytes: {total_bytes/(1024**3):.2f} GB")
    log.info(f"   Tempo estimado: {(total_media * MIN_INTERVAL) / 60:.0f} min (sem rate limits)")

    log.info("")
    log.info("🚀 Iniciando clone...")

    start_time = time.time()

    async with TelegramClient('cloner', API_ID, API_HASH) as client:
        topic_manager = UserTopicManager(client)
        batch_cloner = UserBatchCloner(client, users, topic_manager)

        stats = await batch_cloner.process_all()

    elapsed = time.time() - start_time

    log.info("")
    log.info("=" * 60)
    log.info("🎉 CONCLUÍDO!")
    log.info("=" * 60)
    log.info(f"Usuários processados: {stats['users']}")
    log.info(f"Mídias uploadadas: {stats['uploaded']:,}")
    log.info(f"Falhas: {stats['failed']}")
    log.info(f"Bytes transferidos: {stats['bytes']/(1024**3):.2f} GB")
    log.info(f"Tempo total: {elapsed/60:.1f} minutos")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
