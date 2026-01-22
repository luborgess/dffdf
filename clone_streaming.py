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
import re
import json
from pathlib import Path
from typing import AsyncGenerator, BinaryIO, Optional

# Carregar variáveis de ambiente do arquivo .env
from dotenv import load_dotenv
load_dotenv()

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

# User-to-topic mapping (para organizar mídias por usuário)
USER_TOPIC_MAP_FILE = 'user_topic_map.json'
ORGANIZE_BY_USER = os.environ.get('ORGANIZE_BY_USER', 'false').lower() == 'true'

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
# Tamanho máximo para aplicar watermark (em MB). Acima disso, usa streaming puro.
# Vídeos grandes (ex: 300MB, 20min) demorariam muito no FFmpeg.
# Default: 50MB. Use 0 para desabilitar limite (watermark em todos).
WATERMARK_MAX_SIZE_MB = int(os.environ.get('WATERMARK_MAX_SIZE_MB', '50'))
WATERMARK_MAX_SIZE = WATERMARK_MAX_SIZE_MB * 1024 * 1024  # Converter para bytes

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
    Uma única logo grande no centro com efeito de deslizamento horizontal e 50% de transparência.
    """
    try:
        # Verificar tamanho do arquivo de entrada
        input_size = os.path.getsize(input_path)
        if input_size < 1000:
            log.warning(f"Arquivo de entrada muito pequeno: {input_size} bytes")
            return False

        # Filtro: redimensiona para 40% da largura, adiciona alpha 50%, efeito de deslizamento
        filter_complex = (
            '[1:v]scale=iw*0.40:-1[wm];'
            '[wm]format=yuva420p,colorchannelmixer=aa=0.5[wm_alpha];'
            '[0:v][wm_alpha]overlay=(W-w)/2:(H-h)/2:x=\'if(lt(x,-w),x+w-1,x)\''
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


def generate_video_thumbnail(video_path: str, thumb_path: str, is_preview: bool = False) -> bool:
    """
    Gera thumbnail de vídeo de forma robusta.
    Tenta múltiplos pontos de tempo até conseguir um frame válido.
    
    Para vídeos grandes (is_preview=True), usa seeking após input (-i) para maior precisão,
    pois arquivos de preview podem não ter índice completo.

    Returns:
        True se thumbnail foi gerado com sucesso, False caso contrário.
    """
    # Pontos de tempo para tentar extrair frame (em segundos)
    # Inclui mais pontos para vídeos longos que podem ter keyframes esparsos
    time_points = ['0', '0.5', '1', '2', '3', '5', '10']

    for time_point in time_points:
        try:
            # Para previews de vídeos grandes: -ss DEPOIS de -i (mais preciso, mais lento)
            # Para vídeos completos: -ss ANTES de -i (mais rápido, usa keyframe seeking)
            if is_preview:
                # Modo preciso: decodifica desde o início até o timestamp
                thumb_cmd = [
                    'ffmpeg', '-y',
                    '-i', video_path,
                    '-ss', time_point,
                    '-vframes', '1',
                    '-vf', 'scale=320:-1',
                    '-q:v', '2',
                    thumb_path
                ]
            else:
                # Modo rápido: seek por keyframe
                thumb_cmd = [
                    'ffmpeg', '-y',
                    '-ss', time_point,
                    '-i', video_path,
                    '-vframes', '1',
                    '-vf', 'scale=320:-1',
                    '-q:v', '2',
                    thumb_path
                ]

            result = subprocess.run(
                thumb_cmd,
                capture_output=True,
                timeout=60  # Aumentar timeout para modo preciso
            )

            # Verificar se FFmpeg teve sucesso
            if result.returncode != 0:
                continue

            # Verificar se arquivo foi criado e tem conteúdo válido
            if os.path.exists(thumb_path):
                thumb_size = os.path.getsize(thumb_path)
                if thumb_size > 100:  # Thumbnail válido tem pelo menos 100 bytes
                    log.debug(f"Thumbnail gerado em t={time_point}s: {thumb_size} bytes")
                    return True
                else:
                    # Arquivo muito pequeno, provavelmente inválido
                    os.remove(thumb_path)

        except subprocess.TimeoutExpired:
            log.debug(f"Timeout gerando thumbnail em t={time_point}s")
            continue
        except Exception as e:
            log.debug(f"Erro gerando thumbnail em t={time_point}s: {e}")
            continue

    log.warning(f"Não foi possível gerar thumbnail para {video_path}")
    return False


def add_watermark_image(input_path: str, output_path: str) -> bool:
    """
    Adiciona watermark em imagem usando Pillow.
    Uma única logo grande no centro com 50% de transparência.
    """
    try:
        base = Image.open(input_path).convert('RGBA')
        watermark = Image.open(WATERMARK_PATH).convert('RGBA')

        # Redimensionar watermark para 40% da largura da imagem
        wm_width = int(base.width * 0.40)
        wm_ratio = wm_width / watermark.width
        wm_height = int(watermark.height * wm_ratio)
        watermark = watermark.resize((wm_width, wm_height), Image.Resampling.LANCZOS)

        # Aplicar 50% de transparência
        watermark_alpha = watermark.split()[3]
        watermark_alpha = watermark_alpha.point(lambda p: int(p * 0.5))
        watermark.putalpha(watermark_alpha)

        # Posição centralizada
        pos = (
            (base.width - wm_width) // 2,
            (base.height - wm_height) // 2
        )

        # Aplicar watermark
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
# USER TOPIC MANAGER (Organização por usuário)
# ============================================================

class UserTopicManager:
    """
    Gerencia organização de mídias por usuário.
    Extrai username das legendas e cria/reutiliza tópicos por usuário.
    """

    # Padrões comuns para extrair username de legendas
    USERNAME_PATTERNS = [
        # "⭐ » Username Onlyfans" ou "★ » Username"
        r'[⭐★]\s*»\s*([A-Za-z0-9_.-]+)',
        # "@username"
        r'@([A-Za-z0-9_]+)',
        # "Username - Onlyfans" ou "Username | Onlyfans"
        r'^([A-Za-z0-9_.-]+)\s*[-|]\s*(?:Onlyfans|OF)',
        # "Onlyfans: Username" ou "OF: Username"
        r'(?:Onlyfans|OF)[\s:]+([A-Za-z0-9_.-]+)',
        # Apenas nome no início seguido de quebra de linha ou emoji
        r'^([A-Za-z0-9_.-]{3,30})(?:\s*[\n🔥❤️💦]|$)',
    ]

    def __init__(self, client: TelegramClient):
        self.client = client
        self.user_map: dict[str, int] = {}  # username -> target_topic_id
        self._load_map()

    def _load_map(self):
        """Carrega mapeamento de usuários do arquivo."""
        if os.path.exists(USER_TOPIC_MAP_FILE):
            try:
                with open(USER_TOPIC_MAP_FILE, 'r') as f:
                    data = json.load(f)
                    self.user_map = data.get('users', {})
                log.info(f"👤 Carregado mapeamento de {len(self.user_map)} usuários")
            except Exception as e:
                log.warning(f"Erro ao carregar user_topic_map: {e}")

    def _save_map(self):
        """Salva mapeamento de usuários no arquivo."""
        with open(USER_TOPIC_MAP_FILE, 'w') as f:
            json.dump({'users': self.user_map}, f, indent=2, ensure_ascii=False)

    def extract_username(self, caption: str) -> Optional[str]:
        """
        Extrai username de uma legenda.
        Tenta múltiplos padrões até encontrar um match.
        """
        if not caption:
            return None

        # Limpar e normalizar
        caption = caption.strip()

        for pattern in self.USERNAME_PATTERNS:
            match = re.search(pattern, caption, re.IGNORECASE | re.MULTILINE)
            if match:
                username = match.group(1).strip()
                # Validar: deve ter pelo menos 2 caracteres
                if len(username) >= 2:
                    # Normalizar para lowercase
                    return username.lower()

        return None

    def extract_username_from_messages(self, messages: list) -> Optional[str]:
        """
        Extrai username de uma lista de mensagens (álbum).
        Prioriza a mensagem com legenda mais longa.
        """
        best_caption = ""
        for msg in messages:
            caption = getattr(msg, 'text', '') or getattr(msg, 'message', '') or ''
            if len(caption) > len(best_caption):
                best_caption = caption

        return self.extract_username(best_caption)

    async def get_or_create_user_topic(self, username: str, target_chat: int) -> Optional[int]:
        """
        Retorna o tópico de destino para um usuário.
        Cria automaticamente se não existir.
        """
        if not username:
            return TARGET_TOPIC

        if not FORUM_SUPPORT:
            log.warning("Forum Topics não suportado para criação por usuário")
            return TARGET_TOPIC

        # Já existe no mapa?
        if username in self.user_map:
            return self.user_map[username]

        # Criar tópico no destino
        topic_name = f"📁 {username}"

        try:
            log.info(f"👤 Criando tópico para usuário: '{username}'")

            result = await self.client(CreateForumTopicRequest(
                channel=target_chat,
                title=topic_name,
                icon_color=0xFFD67E,  # Cor dourada para usuários
                random_id=random.randrange(-2**62, 2**62)
            ))

            # O ID do tópico é o ID da primeira mensagem
            new_topic_id = None
            if hasattr(result, 'updates'):
                for update in result.updates:
                    if hasattr(update, 'message') and hasattr(update.message, 'id'):
                        new_topic_id = update.message.id
                        break

            if new_topic_id:
                self.user_map[username] = new_topic_id
                self._save_map()
                log.info(f"✓ Tópico criado para '{username}' (ID: {new_topic_id})")
                return new_topic_id
            else:
                log.error(f"Não foi possível obter ID do tópico para '{username}'")
                return TARGET_TOPIC

        except FloodWaitError as e:
            log.warning(f"FloodWait ao criar tópico de usuário: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            return await self.get_or_create_user_topic(username, target_chat)

        except Exception as e:
            log.error(f"Erro ao criar tópico para '{username}': {e}")
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
    Suporta organização por usuário e envio de álbuns.
    """

    def __init__(self, client: TelegramClient, topic_manager: TopicManager = None,
                 user_topic_manager: UserTopicManager = None):
        self.client = client
        self.topic_manager = topic_manager
        self.user_topic_manager = user_topic_manager
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

        # Se ORGANIZE_BY_USER está ativo, extrair username e criar/usar tópico
        if ORGANIZE_BY_USER and self.user_topic_manager:
            caption = getattr(msg, 'text', '') or getattr(msg, 'message', '') or ''
            username = self.user_topic_manager.extract_username(caption)
            if username:
                target_topic = await self.user_topic_manager.get_or_create_user_topic(
                    username, TARGET_CHAT
                )
                log.debug(f"👤 Mensagem para usuário: {username}")
        elif AUTO_CREATE_TOPICS and self.topic_manager:
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
                
                # ===== MÍDIA GRANDE =====
                if file_size and file_size >= 10 * 1024 * 1024:
                    is_video = msg.video is not None
                    
                    # Watermark em vídeos grandes só se:
                    # 1. Watermark habilitada
                    # 2. É vídeo
                    # 3. Tamanho <= limite (WATERMARK_MAX_SIZE_MB)
                    #    Se limite = 0, aplica em todos (pode ser muito lento!)
                    should_watermark = (
                        WATERMARK_ENABLED and 
                        is_video and 
                        (WATERMARK_MAX_SIZE_MB == 0 or file_size <= WATERMARK_MAX_SIZE)
                    )
                    
                    if should_watermark:
                        return await self._clone_large_video_with_watermark(msg, target_topic)
                    
                    # Streaming puro: vídeos acima do limite ou outros arquivos
                    if is_video and WATERMARK_ENABLED and file_size > WATERMARK_MAX_SIZE:
                        log.info(f"⚠ Vídeo muito grande ({file_size/(1024*1024):.0f}MB > {WATERMARK_MAX_SIZE_MB}MB), streaming sem watermark")
                    
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

    async def clone_album(self, messages: list[Message]) -> bool:
        """
        Clona um álbum (grupo de mídias com mesmo grouped_id).
        Envia todas as mídias juntas com uma única legenda.
        """
        if not messages:
            return False

        if len(messages) == 1:
            # Apenas uma mensagem, usar clone normal
            return await self.clone_message(messages[0])

        await self.wait_rate_limit()

        # Extrair legenda (geralmente na primeira ou última mensagem com texto)
        caption = ""
        for msg in messages:
            if msg.text:
                caption = msg.text
                break

        # Determinar tópico de destino
        target_topic = TARGET_TOPIC

        # Se ORGANIZE_BY_USER está ativo, extrair username e criar/usar tópico
        if ORGANIZE_BY_USER and self.user_topic_manager:
            username = self.user_topic_manager.extract_username_from_messages(messages)
            if username:
                target_topic = await self.user_topic_manager.get_or_create_user_topic(
                    username, TARGET_CHAT
                )
                log.info(f"👤 Álbum para usuário: {username} → tópico {target_topic}")
        elif AUTO_CREATE_TOPICS and self.topic_manager:
            # Fallback para topic_manager se não usar organização por usuário
            source_topic_id = self.topic_manager.get_source_topic_id(messages[0])
            if source_topic_id:
                target_topic = await self.topic_manager.get_or_create_target_topic(
                    source_topic_id, TARGET_CHAT
                )

        try:
            # Download de todas as mídias
            import tempfile
            tmp_dir = tempfile.gettempdir()
            files_to_send = []
            tmp_files = []

            log.info(f"📦 Álbum: {len(messages)} mídias (IDs: {[m.id for m in messages]})")

            for msg in messages:
                file_name = self._get_file_name(msg)
                tmp_path = os.path.join(tmp_dir, f"album_{msg.id}_{file_name}")
                wm_path = os.path.join(tmp_dir, f"wm_album_{msg.id}_{file_name}")
                tmp_files.append(tmp_path)
                tmp_files.append(wm_path)

                await self.client.download_media(msg, file=tmp_path)

                upload_path = tmp_path
                is_video = msg.video is not None
                is_photo = msg.photo is not None

                # Aplicar watermark se habilitado
                if WATERMARK_ENABLED:
                    if is_video:
                        if add_watermark_video(tmp_path, wm_path):
                            upload_path = wm_path
                    elif is_photo:
                        if add_watermark_image(tmp_path, wm_path):
                            upload_path = wm_path

                files_to_send.append(upload_path)

            # Enviar álbum (Telethon agrupa automaticamente quando enviamos lista)
            await self.client.send_file(
                TARGET_CHAT,
                files_to_send,
                reply_to=target_topic
            )

            log.info(f"✓ Álbum: {len(messages)} mídias enviadas")

            # Limpar arquivos temporários
            for tmp_file in tmp_files:
                if os.path.exists(tmp_file):
                    try:
                        os.remove(tmp_file)
                    except:
                        pass

            return True

        except FloodWaitError as e:
            log.warning(f"FloodWait no álbum: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            return await self.clone_album(messages)

        except Exception as e:
            log.error(f"✗ Erro no álbum (IDs: {[m.id for m in messages]}): {e}")
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

                # Gerar thumbnail do vídeo processado (função robusta com múltiplos fallbacks)
                thumb_path = os.path.join(tmp_dir, f"thumb_{file_name}.jpg")
                if not generate_video_thumbnail(upload_path, thumb_path):
                    thumb_path = None

            await self.client.send_file(
                TARGET_CHAT,
                upload_path,
                reply_to=target_topic,
                force_document=False,
                supports_streaming=supports_streaming,
                thumb=thumb_path if thumb_path else None,
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
    
    async def _clone_large_video_with_watermark(self, msg: Message, target_topic: int = None) -> bool:
        """
        Clone de vídeo grande COM watermark.
        
        Requer download completo → processamento FFmpeg → upload.
        Mais lento que streaming puro, mas aplica a marca d'água.
        
        Para vídeos muito grandes, isso pode demorar bastante.
        """
        import tempfile

        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)

        log.info(f"🎬 Grande c/ watermark: {file_name} ({file_size/(1024*1024):.1f}MB)")

        # Diretório temporário com espaço suficiente
        tmp_dir = tempfile.gettempdir()
        tmp_path = os.path.join(tmp_dir, file_name)
        wm_path = os.path.join(tmp_dir, f"wm_{file_name}")
        thumb_path = os.path.join(tmp_dir, f"thumb_{file_name}.jpg")

        try:
            # 1. Download completo do vídeo
            start_time = time.time()
            log.info(f"↓ Baixando vídeo grande...")
            await self.client.download_media(msg, file=tmp_path)
            download_time = time.time() - start_time
            download_speed = file_size / download_time / (1024 * 1024)
            log.info(f"✓ Download: {download_time:.1f}s ({download_speed:.1f} MB/s)")

            # 2. Aplicar watermark com FFmpeg
            log.info(f"🖌 Aplicando watermark em vídeo grande...")
            wm_start = time.time()
            
            upload_path = tmp_path  # Por padrão, enviar original se watermark falhar
            if add_watermark_video(tmp_path, wm_path):
                upload_path = wm_path
                wm_time = time.time() - wm_start
                log.info(f"✓ Watermark aplicada em {wm_time:.1f}s")
            else:
                log.warning(f"⚠ Falha na watermark, enviando original")

            # 3. Gerar thumbnail do vídeo processado
            thumb_generated = generate_video_thumbnail(upload_path, thumb_path, is_preview=False)
            if thumb_generated:
                log.debug(f"✓ Thumbnail gerado")

            # 4. Extrair atributos do vídeo original
            supports_streaming = True
            video_attrs = None
            for attr in (msg.video.attributes if msg.video else []):
                if isinstance(attr, DocumentAttributeVideo):
                    supports_streaming = getattr(attr, 'supports_streaming', True)
                    video_attrs = attr
                    break

            # 5. Upload do vídeo processado
            log.info(f"↑ Enviando vídeo processado...")
            upload_start = time.time()
            
            await self.client.send_file(
                TARGET_CHAT,
                upload_path,
                reply_to=target_topic,
                force_document=False,
                supports_streaming=supports_streaming,
                thumb=thumb_path if thumb_generated else None,
                attributes=[video_attrs] if video_attrs else None
            )
            
            upload_time = time.time() - upload_start
            total_time = time.time() - start_time
            
            log.info(f"✓ Grande c/ watermark: msg {msg.id} (total: {total_time:.1f}s)")
            return True

        except Exception as e:
            log.error(f"✗ Erro processando vídeo grande {msg.id}: {e}")
            return False

        finally:
            # Limpar arquivos temporários
            for path in [tmp_path, wm_path, thumb_path]:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
    
    async def _clone_large_file_streaming(self, msg: Message, target_topic: int = None) -> bool:
        """
        Clone de arquivo grande com STREAMING REAL.
        Download e upload em paralelo, nunca segura arquivo completo.
        Agora também gera thumbnail para vídeos.
        
        NOTA: Watermark não é aplicada em streaming puro por limitação técnica.
        Para vídeos que precisam de watermark, use _clone_large_file_with_watermark.
        """
        import tempfile

        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)
        is_video = msg.video is not None

        log.info(f"⚡ Streaming: {file_name} ({file_size/(1024*1024):.1f}MB)")

        # Criar uploader
        uploader = StreamingUploader(self.client, file_size, file_name)

        # Para vídeos: salvar primeiros chunks para gerar thumbnail
        tmp_dir = tempfile.gettempdir()
        video_preview_path = os.path.join(tmp_dir, f"preview_{file_name}") if is_video else None
        thumb_path = os.path.join(tmp_dir, f"thumb_{file_name}.jpg") if is_video else None
        preview_bytes = 0
        preview_file = None
        thumb_generated = False
        # 10MB para preview - vídeos 16:9 HD/4K podem ter keyframes esparsos
        PREVIEW_SIZE = 10 * 1024 * 1024

        if is_video:
            preview_file = open(video_preview_path, 'wb')

        # Stream download → upload em paralelo
        part_index = 0
        bytes_processed = 0
        start_time = time.time()

        try:
            async for chunk in self.client.iter_download(
                msg.media,
                chunk_size=CHUNK_SIZE,
                request_size=CHUNK_SIZE
            ):
                # Upload chunk (não bloqueia)
                await uploader.upload_chunk(part_index, chunk)

                # Salvar para preview (apenas primeiros chunks de vídeo)
                if is_video and preview_file and preview_bytes < PREVIEW_SIZE:
                    preview_file.write(chunk)
                    preview_bytes += len(chunk)

                    # Quando temos dados suficientes, gerar thumbnail
                    if preview_bytes >= PREVIEW_SIZE and not thumb_generated:
                        preview_file.close()
                        preview_file = None
                        log.debug(f"Gerando thumbnail de vídeo grande (preview={preview_bytes/(1024*1024):.1f}MB)...")
                        # is_preview=True usa seeking preciso (mais lento, mas funciona com arquivos parciais)
                        thumb_generated = generate_video_thumbnail(video_preview_path, thumb_path, is_preview=True)
                        if thumb_generated:
                            log.debug(f"✓ Thumbnail gerado para vídeo grande")

                bytes_processed += len(chunk)
                part_index += 1

                # Log progresso a cada 10%
                progress = bytes_processed / file_size * 100
                if int(progress) % 10 == 0 and int(progress) > 0:
                    elapsed = time.time() - start_time
                    speed = bytes_processed / elapsed / (1024 * 1024)
                    log.debug(f"  {progress:.0f}% ({speed:.1f} MB/s)")

            # Fechar preview file se ainda aberto (vídeos muito pequenos em streaming)
            if preview_file:
                preview_file.close()
                preview_file = None
                # Tentar gerar thumbnail com o que temos
                if is_video and not thumb_generated and preview_bytes > 0:
                    thumb_generated = generate_video_thumbnail(video_preview_path, thumb_path, is_preview=True)

            # Aguardar uploads pendentes
            await uploader.wait_completion()

            # Fazer upload do thumbnail se gerado
            thumb_input_file = None
            if thumb_generated and thumb_path and os.path.exists(thumb_path):
                try:
                    thumb_input_file = await self.client.upload_file(thumb_path)
                except Exception as e:
                    log.warning(f"Falha no upload do thumbnail: {e}")
                    thumb_input_file = None

            # Finalizar: enviar mensagem com o arquivo
            input_file = uploader.get_input_file()

            # Criar InputMedia baseado no tipo (com thumbnail se disponível)
            media = self._create_input_media(msg, input_file, thumb=thumb_input_file)

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

        finally:
            # Limpar arquivos temporários
            if preview_file:
                preview_file.close()
            if video_preview_path and os.path.exists(video_preview_path):
                os.remove(video_preview_path)
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)
    
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
    
    def _create_input_media(self, msg: Message, input_file: InputFileBig, thumb=None):
        """Cria InputMedia baseado no tipo original, com suporte a thumbnail."""

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
            thumb=thumb,
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
    log.info(f"Organizar por usuário: {'SIM' if ORGANIZE_BY_USER else 'NÃO'}")
    log.info("=" * 60)

    last_id = load_checkpoint()
    if last_id:
        log.info(f"Resumindo de msg {last_id}")

    stats = {'ok': 0, 'fail': 0, 'bytes': 0, 'albums': 0}
    start_time = time.time()

    async with TelegramClient('cloner', API_ID, API_HASH) as client:

        # Inicializar Topic Manager
        topic_manager = None
        if AUTO_CREATE_TOPICS and FORUM_SUPPORT and not ORGANIZE_BY_USER:
            log.info("Topic Manager: ATIVADO")
            topic_manager = TopicManager(client)
            await topic_manager.load_source_topics(SOURCE_CHAT)
        elif AUTO_CREATE_TOPICS and not FORUM_SUPPORT:
            log.warning("AUTO_CREATE_TOPICS configurado mas Forum não suportado - ignorando")

        # Inicializar User Topic Manager
        user_topic_manager = None
        if ORGANIZE_BY_USER and FORUM_SUPPORT:
            log.info("👤 User Topic Manager: ATIVADO")
            user_topic_manager = UserTopicManager(client)
        elif ORGANIZE_BY_USER and not FORUM_SUPPORT:
            log.warning("ORGANIZE_BY_USER configurado mas Forum não suportado - ignorando")

        cloner = StreamingCloner(
            client,
            topic_manager=topic_manager,
            user_topic_manager=user_topic_manager
        )

        log.info("Conectado! Buscando mensagens...")

        # Agrupar mensagens por grouped_id para detectar álbuns
        current_album: list[Message] = []
        current_grouped_id: int | None = None

        async def flush_album():
            """Processa e envia o álbum atual."""
            nonlocal current_album, stats

            if not current_album:
                return

            if len(current_album) == 1:
                # Mensagem individual
                msg = current_album[0]
                success = await cloner.clone_message(msg)
                if success:
                    stats['ok'] += 1
                    stats['bytes'] += cloner._get_file_size(msg) or 0
                else:
                    stats['fail'] += 1
                save_checkpoint(msg.id)
            else:
                # Álbum de mídias
                success = await cloner.clone_album(current_album)
                if success:
                    stats['ok'] += len(current_album)
                    stats['albums'] += 1
                    for msg in current_album:
                        stats['bytes'] += cloner._get_file_size(msg) or 0
                else:
                    stats['fail'] += len(current_album)
                # Salvar checkpoint do último ID do álbum
                save_checkpoint(current_album[-1].id)

            current_album = []

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

            # Verificar se é parte de um álbum
            grouped_id = getattr(msg, 'grouped_id', None)

            if grouped_id is None:
                # Mensagem individual - processar álbum anterior primeiro
                await flush_album()
                current_album = [msg]
                current_grouped_id = None
                await flush_album()

            elif grouped_id == current_grouped_id:
                # Mesma album - adicionar à lista
                current_album.append(msg)

            else:
                # Novo álbum - processar anterior e iniciar novo
                await flush_album()
                current_album = [msg]
                current_grouped_id = grouped_id

            # Log a cada 10 mensagens processadas
            total = stats['ok'] + stats['fail']
            if total > 0 and total % 10 == 0:
                elapsed = (time.time() - start_time) / 60
                rate = total / elapsed if elapsed > 0 else 0
                gb = stats['bytes'] / (1024**3)
                log.info(
                    f"Progresso: {stats['ok']} ok | {stats['albums']} álbuns | "
                    f"{rate:.1f} msg/min | {gb:.2f} GB"
                )

        # Processar último álbum pendente
        await flush_album()

    elapsed = (time.time() - start_time) / 60
    log.info("=" * 60)
    log.info("CONCLUÍDO!")
    log.info(f"Sucesso: {stats['ok']} mensagens")
    log.info(f"Álbuns: {stats['albums']}")
    log.info(f"Falhas: {stats['fail']}")
    log.info(f"Transferido: {stats['bytes']/(1024**3):.2f} GB")
    log.info(f"Tempo: {elapsed:.1f} minutos")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
