#!/usr/bin/env python3
"""
Clone de grupo Telegram com STREAMING REAL + CHECKPOINT COMPARTILHADO.

Versão multi-sessão com SQLite para coordenação entre múltiplas
instâncias do script enviando para o mesmo target.

Usa:
- SQLite para checkpoint atômico (evita duplicação)
- Lock por mensagem (uma sessão reserva, processa, marca como feita)
- Suporte a múltiplos source chats → um target chat
"""

import asyncio
import os
import time
import logging
import hashlib
import random
import sqlite3
import re
import json
from pathlib import Path
from typing import AsyncGenerator, BinaryIO, Optional
from contextlib import contextmanager

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

# Session name único para esta instância
SESSION_NAME = os.environ.get('SESSION_NAME', 'session')

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

# ============================================================
# CHECKPOINT SQLITE COMPARTILHADO
# ============================================================

# Caminho para o banco compartilhado
# Usar caminho relativo para ../shared/ ou variável de ambiente
SHARED_DB_PATH = os.environ.get('SHARED_DB_PATH', '../shared/checkpoint.db')

class SharedCheckpoint:
    """
    Checkpoint compartilhado usando SQLite.
    
    Permite que múltiplas sessões trabalhem no mesmo target sem duplicação.
    Cada mensagem é marcada com status:
    - NULL: não processada
    - 'processing': sendo processada (lock)
    - 'done': concluída
    - 'failed': falhou
    """
    
    def __init__(self, db_path: str = SHARED_DB_PATH):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Inicializa banco de dados."""
        with self._get_conn() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    source_chat INTEGER NOT NULL,
                    msg_id INTEGER NOT NULL,
                    status TEXT DEFAULT NULL,
                    session TEXT DEFAULT NULL,
                    processed_at TIMESTAMP DEFAULT NULL,
                    target_msg_id INTEGER DEFAULT NULL,
                    PRIMARY KEY (source_chat, msg_id)
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_status
                ON messages(source_chat, status)
            ''')
            # Tabela para mapeamento de usuários (para ORGANIZE_BY_USER)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS user_topics (
                    username TEXT PRIMARY KEY,
                    topic_id INTEGER DEFAULT NULL,
                    status TEXT DEFAULT NULL,
                    session TEXT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
    
    @contextmanager
    def _get_conn(self):
        """Conexão com timeout e WAL mode para concorrência."""
        conn = sqlite3.connect(
            self.db_path, 
            timeout=30.0,
            isolation_level='IMMEDIATE'
        )
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
        try:
            yield conn
        finally:
            conn.close()
    
    def try_lock_message(self, source_chat: int, msg_id: int, session: str) -> bool:
        """
        Tenta fazer lock de uma mensagem para processamento.
        
        Returns:
            True se conseguiu o lock (pode processar)
            False se já está em processamento ou concluída
        """
        with self._get_conn() as conn:
            try:
                # Tentar inserir novo registro com status 'processing'
                cursor = conn.execute('''
                    INSERT INTO messages (source_chat, msg_id, status, session)
                    VALUES (?, ?, 'processing', ?)
                    ON CONFLICT(source_chat, msg_id) DO UPDATE SET
                        status = CASE 
                            WHEN messages.status IS NULL THEN 'processing'
                            WHEN messages.status = 'failed' THEN 'processing'
                            ELSE messages.status
                        END,
                        session = CASE
                            WHEN messages.status IS NULL THEN excluded.session
                            WHEN messages.status = 'failed' THEN excluded.session
                            ELSE messages.session
                        END
                    WHERE messages.status IS NULL OR messages.status = 'failed'
                ''', (source_chat, msg_id, session))
                
                conn.commit()
                
                # Verificar se realmente conseguimos o lock
                cursor = conn.execute('''
                    SELECT status, session FROM messages 
                    WHERE source_chat = ? AND msg_id = ?
                ''', (source_chat, msg_id))
                
                row = cursor.fetchone()
                if row:
                    status, locked_session = row
                    return status == 'processing' and locked_session == session
                
                return False
                
            except sqlite3.IntegrityError:
                # Já existe, verificar status
                return False
    
    def mark_done(self, source_chat: int, msg_id: int, target_msg_id: int = None):
        """Marca mensagem como processada com sucesso."""
        with self._get_conn() as conn:
            conn.execute('''
                UPDATE messages 
                SET status = 'done', 
                    processed_at = datetime('now'),
                    target_msg_id = ?
                WHERE source_chat = ? AND msg_id = ?
            ''', (target_msg_id, source_chat, msg_id))
            conn.commit()
    
    def mark_failed(self, source_chat: int, msg_id: int):
        """Marca mensagem como falha (pode ser reprocessada)."""
        with self._get_conn() as conn:
            conn.execute('''
                UPDATE messages 
                SET status = 'failed', processed_at = datetime('now')
                WHERE source_chat = ? AND msg_id = ?
            ''', (source_chat, msg_id))
            conn.commit()
    
    def is_processed(self, source_chat: int, msg_id: int) -> bool:
        """Verifica se mensagem já foi processada."""
        with self._get_conn() as conn:
            cursor = conn.execute('''
                SELECT status FROM messages 
                WHERE source_chat = ? AND msg_id = ?
            ''', (source_chat, msg_id))
            row = cursor.fetchone()
            return row is not None and row[0] == 'done'
    
    def get_last_processed(self, source_chat: int) -> int:
        """Retorna o último msg_id processado com sucesso."""
        with self._get_conn() as conn:
            cursor = conn.execute('''
                SELECT MAX(msg_id) FROM messages 
                WHERE source_chat = ? AND status = 'done'
            ''', (source_chat,))
            row = cursor.fetchone()
            return row[0] if row and row[0] else 0
    
    def get_or_create_user_topic(self, username: str, session: str) -> tuple[int | None, bool]:
        """
        Obtém ou reserva um tópico para um usuário de forma atômica.

        Returns:
            (topic_id, is_new): topic_id existente ou None se precisa criar, is_new indica se é novo
        """
        with self._get_conn() as conn:
            # Verificar se já existe
            cursor = conn.execute('''
                SELECT topic_id FROM user_topics WHERE username = ?
            ''', (username,))
            row = cursor.fetchone()

            if row and row[0]:
                return row[0], False

            # Tentar reservar para criação
            try:
                conn.execute('''
                    INSERT INTO user_topics (username, status, session)
                    VALUES (?, 'creating', ?)
                    ON CONFLICT(username) DO UPDATE SET
                        status = CASE
                            WHEN user_topics.status IS NULL THEN 'creating'
                            ELSE user_topics.status
                        END,
                        session = CASE
                            WHEN user_topics.status IS NULL THEN excluded.session
                            ELSE user_topics.session
                        END
                    WHERE user_topics.topic_id IS NULL
                ''', (username, session))
                conn.commit()

                # Verificar se conseguimos o lock
                cursor = conn.execute('''
                    SELECT status, session FROM user_topics WHERE username = ?
                ''', (username,))
                row = cursor.fetchone()
                if row and row[0] == 'creating' and row[1] == session:
                    return None, True  # Precisa criar

                # Outra sessão está criando, esperar
                return None, False

            except sqlite3.IntegrityError:
                return None, False

    def set_user_topic(self, username: str, topic_id: int):
        """Define o topic_id para um usuário após criação."""
        with self._get_conn() as conn:
            conn.execute('''
                UPDATE user_topics
                SET topic_id = ?, status = 'done'
                WHERE username = ?
            ''', (topic_id, username))
            conn.commit()

    def get_user_topic(self, username: str) -> int | None:
        """Obtém o topic_id de um usuário."""
        with self._get_conn() as conn:
            cursor = conn.execute('''
                SELECT topic_id FROM user_topics WHERE username = ? AND topic_id IS NOT NULL
            ''', (username,))
            row = cursor.fetchone()
            return row[0] if row else None

    def get_stats(self, source_chat: int = None) -> dict:
        """Retorna estatísticas do checkpoint."""
        with self._get_conn() as conn:
            if source_chat:
                cursor = conn.execute('''
                    SELECT status, COUNT(*) FROM messages 
                    WHERE source_chat = ?
                    GROUP BY status
                ''', (source_chat,))
            else:
                cursor = conn.execute('''
                    SELECT status, COUNT(*) FROM messages 
                    GROUP BY status
                ''')
            
            stats = {'done': 0, 'processing': 0, 'failed': 0}
            for row in cursor:
                if row[0]:
                    stats[row[0]] = row[1]
            return stats
    
    def cleanup_stale_locks(self, max_age_minutes: int = 30):
        """
        Limpa locks antigos (sessões que morreram).
        Mensagens em 'processing' há mais de X minutos voltam para NULL.
        """
        with self._get_conn() as conn:
            conn.execute('''
                UPDATE messages 
                SET status = 'failed', session = NULL
                WHERE status = 'processing' 
                AND processed_at < datetime('now', ?)
            ''', (f'-{max_age_minutes} minutes',))
            conn.commit()


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
    format=f'%(asctime)s | {SESSION_NAME} | %(levelname)s | %(message)s',
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


def generate_video_thumbnail(video_path: str, thumb_path: str, is_preview: bool = False) -> bool:
    """
    Gera thumbnail de vídeo de forma robusta.
    Tenta múltiplos pontos de tempo até conseguir um frame válido.
    
    Para vídeos grandes (is_preview=True), usa seeking após input (-i) para maior precisão,
    pois arquivos de preview podem não ter índice completo.
    """
    # Pontos de tempo para tentar extrair frame (inclui mais pontos para vídeos longos)
    time_points = ['0', '0.5', '1', '2', '3', '5', '10']

    for time_point in time_points:
        try:
            # Para previews de vídeos grandes: -ss DEPOIS de -i (mais preciso, mais lento)
            # Para vídeos completos: -ss ANTES de -i (mais rápido, usa keyframe seeking)
            if is_preview:
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
                thumb_cmd = [
                    'ffmpeg', '-y',
                    '-ss', time_point,
                    '-i', video_path,
                    '-vframes', '1',
                    '-vf', 'scale=320:-1',
                    '-q:v', '2',
                    thumb_path
                ]

            result = subprocess.run(thumb_cmd, capture_output=True, timeout=60)

            if result.returncode != 0:
                continue

            if os.path.exists(thumb_path):
                thumb_size = os.path.getsize(thumb_path)
                if thumb_size > 100:
                    log.debug(f"Thumbnail gerado em t={time_point}s: {thumb_size} bytes")
                    return True
                else:
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
    """Adiciona watermark em imagem usando Pillow."""
    try:
        base = Image.open(input_path).convert('RGBA')
        watermark = Image.open(WATERMARK_PATH).convert('RGBA')

        wm_width = int(base.width * 0.225)
        wm_ratio = wm_width / watermark.width
        wm_height = int(watermark.height * wm_ratio)
        watermark = watermark.resize((wm_width, wm_height), Image.Resampling.LANCZOS)

        positions = [
            (10, 10),
            (base.width - wm_width - 10, base.height - wm_height - 10),
        ]

        for pos in positions:
            base.paste(watermark, pos, watermark)

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
    """Gerencia criação automática de tópicos no destino."""
    
    def __init__(self, client: TelegramClient):
        self.client = client
        self.topic_map: dict[int, int] = {}
        self.source_topics: dict[int, str] = {}
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
        if hasattr(msg, 'reply_to') and msg.reply_to:
            if hasattr(msg.reply_to, 'reply_to_top_id') and msg.reply_to.reply_to_top_id:
                return msg.reply_to.reply_to_top_id
            if hasattr(msg.reply_to, 'reply_to_msg_id') and msg.reply_to.reply_to_msg_id:
                if msg.reply_to.reply_to_msg_id in self.source_topics:
                    return msg.reply_to.reply_to_msg_id
        return None
    
    async def get_or_create_target_topic(self, source_topic_id: int, target_chat: int) -> int | None:
        """Retorna o tópico de destino correspondente."""
        if not source_topic_id:
            return TARGET_TOPIC
        
        if not FORUM_SUPPORT:
            return TARGET_TOPIC
        
        if source_topic_id in self.topic_map:
            return self.topic_map[source_topic_id]
        
        topic_name = self.source_topics.get(source_topic_id, f"Tópico {source_topic_id}")
        
        try:
            log.info(f"📝 Criando tópico no destino: '{topic_name}'")
            
            result = await self.client(CreateForumTopicRequest(
                channel=target_chat,
                title=topic_name,
                icon_color=0x6FB9F0,
                random_id=random.randrange(-2**62, 2**62)
            ))
            
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
    Usa SharedCheckpoint (SQLite) para coordenação entre múltiplas sessões.
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

    def __init__(self, client: TelegramClient, checkpoint: SharedCheckpoint):
        self.client = client
        self.checkpoint = checkpoint
        self.local_cache: dict[str, int] = {}  # Cache local para reduzir queries

    def extract_username(self, caption: str) -> Optional[str]:
        """
        Extrai username de uma legenda.
        Tenta múltiplos padrões até encontrar um match.
        """
        if not caption:
            return None

        caption = caption.strip()

        for pattern in self.USERNAME_PATTERNS:
            match = re.search(pattern, caption, re.IGNORECASE | re.MULTILINE)
            if match:
                username = match.group(1).strip()
                if len(username) >= 2:
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
        Usa SQLite compartilhado para evitar duplicação entre sessões.
        """
        if not username:
            return TARGET_TOPIC

        if not FORUM_SUPPORT:
            log.warning("Forum Topics não suportado para criação por usuário")
            return TARGET_TOPIC

        # Cache local
        if username in self.local_cache:
            return self.local_cache[username]

        # Verificar no banco compartilhado
        existing_topic = self.checkpoint.get_user_topic(username)
        if existing_topic:
            self.local_cache[username] = existing_topic
            return existing_topic

        # Tentar reservar para criação (atômico)
        topic_id, should_create = self.checkpoint.get_or_create_user_topic(username, SESSION_NAME)

        if topic_id:
            # Já existe
            self.local_cache[username] = topic_id
            return topic_id

        if not should_create:
            # Outra sessão está criando, aguardar
            for _ in range(10):  # Max 10 tentativas
                await asyncio.sleep(1)
                topic_id = self.checkpoint.get_user_topic(username)
                if topic_id:
                    self.local_cache[username] = topic_id
                    return topic_id
            log.warning(f"Timeout aguardando criação de tópico para '{username}'")
            return TARGET_TOPIC

        # Criar tópico
        topic_name = f"📁 {username}"

        try:
            log.info(f"👤 Criando tópico para usuário: '{username}'")

            result = await self.client(CreateForumTopicRequest(
                channel=target_chat,
                title=topic_name,
                icon_color=0xFFD67E,  # Cor dourada para usuários
                random_id=random.randrange(-2**62, 2**62)
            ))

            new_topic_id = None
            if hasattr(result, 'updates'):
                for update in result.updates:
                    if hasattr(update, 'message') and hasattr(update.message, 'id'):
                        new_topic_id = update.message.id
                        break

            if new_topic_id:
                self.checkpoint.set_user_topic(username, new_topic_id)
                self.local_cache[username] = new_topic_id
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
    """Upload de arquivo grande em streaming."""
    
    def __init__(self, client: TelegramClient, file_size: int, file_name: str):
        self.client = client
        self.file_size = file_size
        self.file_name = file_name
        self.file_id = random.randrange(-2**62, 2**62)
        self.total_parts = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        self.parts_uploaded = 0
        self.md5_hash = hashlib.md5()
        self.semaphore = asyncio.Semaphore(PARALLEL_UPLOADS)
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
# CLONE COM STREAMING + CHECKPOINT COMPARTILHADO
# ============================================================

class StreamingCloner:
    """
    Clonador com streaming real + checkpoint SQLite compartilhado.
    Múltiplas sessões podem trabalhar sem duplicação.
    Suporta organização por usuário e envio de álbuns.
    """

    def __init__(self, client: TelegramClient, checkpoint: SharedCheckpoint,
                 topic_manager: TopicManager = None, user_topic_manager: UserTopicManager = None):
        self.client = client
        self.checkpoint = checkpoint
        self.topic_manager = topic_manager
        self.user_topic_manager = user_topic_manager
        self.last_send_time = 0
    
    async def wait_rate_limit(self):
        """Aguarda rate limit."""
        elapsed = time.time() - self.last_send_time
        if elapsed < MIN_INTERVAL:
            await asyncio.sleep(MIN_INTERVAL - elapsed)
        self.last_send_time = time.time()
    
    async def clone_message(self, msg: Message) -> bool:
        """Clona uma mensagem com streaming e checkpoint compartilhado."""

        # Tentar fazer lock da mensagem
        if not self.checkpoint.try_lock_message(SOURCE_CHAT, msg.id, SESSION_NAME):
            log.debug(f"⊘ Msg {msg.id} já em processamento ou concluída")
            return False

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
                result = await self.client.send_message(
                    TARGET_CHAT,
                    msg.text,
                    reply_to=target_topic
                )
                self.checkpoint.mark_done(SOURCE_CHAT, msg.id, result.id if result else None)
                log.info(f"✓ Texto: msg {msg.id}")
                return True
            
            # ===== MÍDIA PEQUENA (<10MB) =====
            if msg.media:
                file_size = self._get_file_size(msg)
                
                if file_size and file_size < 10 * 1024 * 1024:
                    success = await self._clone_small_file(msg, target_topic)
                    if success:
                        self.checkpoint.mark_done(SOURCE_CHAT, msg.id)
                    else:
                        self.checkpoint.mark_failed(SOURCE_CHAT, msg.id)
                    return success
                
                # ===== MÍDIA GRANDE =====
                if file_size and file_size >= 10 * 1024 * 1024:
                    is_video = msg.video is not None
                    
                    # Watermark em vídeos grandes só se:
                    # 1. Watermark habilitada
                    # 2. É vídeo
                    # 3. Tamanho <= limite (WATERMARK_MAX_SIZE_MB)
                    should_watermark = (
                        WATERMARK_ENABLED and 
                        is_video and 
                        (WATERMARK_MAX_SIZE_MB == 0 or file_size <= WATERMARK_MAX_SIZE)
                    )
                    
                    if should_watermark:
                        success = await self._clone_large_video_with_watermark(msg, target_topic)
                    else:
                        # Streaming puro: vídeos acima do limite ou outros arquivos
                        if is_video and WATERMARK_ENABLED and file_size > WATERMARK_MAX_SIZE:
                            log.info(f"⚠ Vídeo muito grande ({file_size/(1024*1024):.0f}MB > {WATERMARK_MAX_SIZE_MB}MB), streaming sem watermark")
                        success = await self._clone_large_file_streaming(msg, target_topic)
                    
                    if success:
                        self.checkpoint.mark_done(SOURCE_CHAT, msg.id)
                    else:
                        self.checkpoint.mark_failed(SOURCE_CHAT, msg.id)
                    return success
            
            log.warning(f"⊘ Tipo não suportado: msg {msg.id}")
            self.checkpoint.mark_failed(SOURCE_CHAT, msg.id)
            return False
            
        except FloodWaitError as e:
            log.warning(f"FloodWait: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
            # Não marcar como falha, vai tentar de novo
            return await self.clone_message(msg)
            
        except Exception as e:
            log.error(f"✗ Erro msg {msg.id}: {e}")
            self.checkpoint.mark_failed(SOURCE_CHAT, msg.id)
            return False

    async def clone_album(self, messages: list[Message]) -> bool:
        """
        Clona um álbum (grupo de mídias com mesmo grouped_id).
        Envia todas as mídias juntas com uma única legenda.
        Usa checkpoint compartilhado para coordenação.
        """
        if not messages:
            return False

        if len(messages) == 1:
            return await self.clone_message(messages[0])

        # Tentar lock de TODAS as mensagens do álbum
        locked_ids = []
        for msg in messages:
            if self.checkpoint.try_lock_message(SOURCE_CHAT, msg.id, SESSION_NAME):
                locked_ids.append(msg.id)
            elif not self.checkpoint.is_processed(SOURCE_CHAT, msg.id):
                # Já está sendo processada por outra sessão
                # Liberar locks que já fizemos
                for lid in locked_ids:
                    self.checkpoint.mark_failed(SOURCE_CHAT, lid)
                log.debug(f"⊘ Álbum {[m.id for m in messages]} já em processamento")
                return False

        # Se não conseguiu lock de nenhuma (todas já processadas)
        if not locked_ids:
            return False

        await self.wait_rate_limit()

        # Extrair legenda
        caption = ""
        for msg in messages:
            if msg.text:
                caption = msg.text
                break

        # Determinar tópico de destino
        target_topic = TARGET_TOPIC

        if ORGANIZE_BY_USER and self.user_topic_manager:
            username = self.user_topic_manager.extract_username_from_messages(messages)
            if username:
                target_topic = await self.user_topic_manager.get_or_create_user_topic(
                    username, TARGET_CHAT
                )
                log.info(f"👤 Álbum para usuário: {username} → tópico {target_topic}")
        elif AUTO_CREATE_TOPICS and self.topic_manager:
            source_topic_id = self.topic_manager.get_source_topic_id(messages[0])
            if source_topic_id:
                target_topic = await self.topic_manager.get_or_create_target_topic(
                    source_topic_id, TARGET_CHAT
                )

        try:
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

                if WATERMARK_ENABLED:
                    if is_video:
                        if add_watermark_video(tmp_path, wm_path):
                            upload_path = wm_path
                    elif is_photo:
                        if add_watermark_image(tmp_path, wm_path):
                            upload_path = wm_path

                files_to_send.append(upload_path)

            # Enviar álbum
            await self.client.send_file(
                TARGET_CHAT,
                files_to_send,
                caption=caption,
                reply_to=target_topic
            )

            log.info(f"✓ Álbum: {len(messages)} mídias enviadas")

            # Marcar todas como done
            for msg in messages:
                self.checkpoint.mark_done(SOURCE_CHAT, msg.id)

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
            for msg in messages:
                self.checkpoint.mark_failed(SOURCE_CHAT, msg.id)
            return False

    async def _clone_small_file(self, msg: Message, target_topic: int = None) -> bool:
        """Clone de arquivo pequeno (cabe em RAM)."""
        import tempfile

        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)

        log.info(f"↓↑ Pequeno: {file_name} ({file_size/(1024*1024):.1f}MB)")

        tmp_dir = tempfile.gettempdir()
        tmp_path = os.path.join(tmp_dir, file_name)
        wm_path = os.path.join(tmp_dir, f"wm_{file_name}")

        try:
            await self.client.download_media(msg, file=tmp_path)

            is_video = msg.video is not None
            is_photo = msg.photo is not None
            supports_streaming = False
            upload_path = tmp_path

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

            video_attrs = None
            thumb_path = None
            if is_video:
                for attr in (msg.video.attributes if msg.video else []):
                    if isinstance(attr, DocumentAttributeVideo):
                        supports_streaming = getattr(attr, 'supports_streaming', True)
                        video_attrs = attr
                        break

                thumb_path = os.path.join(tmp_dir, f"thumb_{file_name}.jpg")
                if not generate_video_thumbnail(upload_path, thumb_path):
                    thumb_path = None

            await self.client.send_file(
                TARGET_CHAT,
                upload_path,
                caption=msg.text or "",
                reply_to=target_topic,
                force_document=False,
                supports_streaming=supports_streaming,
                thumb=thumb_path if thumb_path else None,
                attributes=[video_attrs] if video_attrs else None
            )

            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)

            log.info(f"✓ Pequeno: msg {msg.id}")
            return True

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if os.path.exists(wm_path):
                os.remove(wm_path)
    
    async def _clone_large_video_with_watermark(self, msg: Message, target_topic: int = None) -> bool:
        """
        Clone de vídeo grande COM watermark.
        
        Requer download completo → processamento FFmpeg → upload.
        Mais lento que streaming puro, mas aplica a marca d'água.
        """
        import tempfile

        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)

        log.info(f"🎬 Grande c/ watermark: {file_name} ({file_size/(1024*1024):.1f}MB)")

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
            
            upload_path = tmp_path
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
                caption=msg.text or "",
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
            for path in [tmp_path, wm_path, thumb_path]:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
    
    async def _clone_large_file_streaming(self, msg: Message, target_topic: int = None) -> bool:
        """Clone de arquivo grande com STREAMING REAL."""
        import tempfile

        file_name = self._get_file_name(msg)
        file_size = self._get_file_size(msg)
        is_video = msg.video is not None

        log.info(f"⚡ Streaming: {file_name} ({file_size/(1024*1024):.1f}MB)")

        uploader = StreamingUploader(self.client, file_size, file_name)

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
                        log.debug(f"Gerando thumbnail de vídeo grande (preview={preview_bytes/(1024*1024):.1f}MB)...")
                        thumb_generated = generate_video_thumbnail(video_preview_path, thumb_path, is_preview=True)
                        if thumb_generated:
                            log.debug(f"✓ Thumbnail gerado para vídeo grande")

                bytes_processed += len(chunk)
                part_index += 1

                progress = bytes_processed / file_size * 100
                if int(progress) % 10 == 0 and int(progress) > 0:
                    elapsed = time.time() - start_time
                    speed = bytes_processed / elapsed / (1024 * 1024)
                    log.debug(f"  {progress:.0f}% ({speed:.1f} MB/s)")

            if preview_file:
                preview_file.close()
                preview_file = None
                if is_video and not thumb_generated and preview_bytes > 0:
                    thumb_generated = generate_video_thumbnail(video_preview_path, thumb_path, is_preview=True)

            await uploader.wait_completion()

            thumb_input_file = None
            if thumb_generated and thumb_path and os.path.exists(thumb_path):
                try:
                    thumb_input_file = await self.client.upload_file(thumb_path)
                except Exception as e:
                    log.warning(f"Falha no upload do thumbnail: {e}")
                    thumb_input_file = None

            input_file = uploader.get_input_file()
            media = self._create_input_media(msg, input_file, thumb=thumb_input_file)

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
    
    def _create_input_media(self, msg: Message, input_file: InputFileBig, thumb=None):
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
            thumb=thumb,
            force_file=False
        )


# ============================================================
# MAIN
# ============================================================

async def main():
    log.info("=" * 60)
    log.info(f"TELEGRAM STREAMING CLONER - {SESSION_NAME}")
    log.info("=" * 60)
    log.info(f"Origem: {SOURCE_CHAT} (tópico: {SOURCE_TOPIC})")
    log.info(f"Destino: {TARGET_CHAT} (tópico: {TARGET_TOPIC})")
    log.info(f"Checkpoint DB: {os.path.abspath(SHARED_DB_PATH)}")
    log.info(f"Chunk size: {CHUNK_SIZE // 1024}KB")
    log.info(f"Organizar por usuário: {'SIM' if ORGANIZE_BY_USER else 'NÃO'}")
    log.info("=" * 60)

    # Inicializar checkpoint compartilhado
    checkpoint = SharedCheckpoint(SHARED_DB_PATH)

    # Limpar locks antigos (sessões mortas)
    checkpoint.cleanup_stale_locks(max_age_minutes=30)

    # Estatísticas iniciais
    stats_db = checkpoint.get_stats(SOURCE_CHAT)
    log.info(f"Checkpoint: {stats_db['done']} feitas | {stats_db['processing']} em andamento | {stats_db['failed']} falhas")

    stats = {'ok': 0, 'fail': 0, 'skip': 0, 'bytes': 0, 'albums': 0}
    start_time = time.time()

    async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:

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
            log.info("👤 User Topic Manager: ATIVADO (SQLite compartilhado)")
            user_topic_manager = UserTopicManager(client, checkpoint)
        elif ORGANIZE_BY_USER and not FORUM_SUPPORT:
            log.warning("ORGANIZE_BY_USER configurado mas Forum não suportado - ignorando")

        cloner = StreamingCloner(
            client, checkpoint,
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

            # Verificar se todas já foram processadas
            all_processed = all(
                checkpoint.is_processed(SOURCE_CHAT, m.id) for m in current_album
            )
            if all_processed:
                stats['skip'] += len(current_album)
                current_album = []
                return

            if len(current_album) == 1:
                msg = current_album[0]
                if checkpoint.is_processed(SOURCE_CHAT, msg.id):
                    stats['skip'] += 1
                else:
                    success = await cloner.clone_message(msg)
                    if success:
                        stats['ok'] += 1
                        stats['bytes'] += cloner._get_file_size(msg) or 0
                    elif not checkpoint.is_processed(SOURCE_CHAT, msg.id):
                        stats['fail'] += 1
            else:
                success = await cloner.clone_album(current_album)
                if success:
                    stats['ok'] += len(current_album)
                    stats['albums'] += 1
                    for msg in current_album:
                        stats['bytes'] += cloner._get_file_size(msg) or 0
                elif not all(checkpoint.is_processed(SOURCE_CHAT, m.id) for m in current_album):
                    stats['fail'] += len(current_album)

            current_album = []

        async for msg in client.iter_messages(
            SOURCE_CHAT,
            min_id=0,  # Começar do início, checkpoint vai filtrar
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
                # Mesmo álbum - adicionar à lista
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
                    f"Progresso: {stats['ok']} ok | {stats['albums']} álbuns | {stats['skip']} skip | "
                    f"{rate:.1f} msg/min | {gb:.2f} GB"
                )

        # Processar último álbum pendente
        await flush_album()

    elapsed = (time.time() - start_time) / 60
    log.info("=" * 60)
    log.info("CONCLUÍDO!")
    log.info(f"Sucesso: {stats['ok']} mensagens")
    log.info(f"Álbuns: {stats['albums']}")
    log.info(f"Puladas (já feitas): {stats['skip']}")
    log.info(f"Falhas: {stats['fail']}")
    log.info(f"Transferido: {stats['bytes']/(1024**3):.2f} GB")
    log.info(f"Tempo: {elapsed:.1f} minutos")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
