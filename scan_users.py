#!/usr/bin/env python3
"""
Script de scan para contar mídias por usuário.
Identifica usuários com maiores acervos para focar em processamento futuro.
"""

import asyncio
import os
import time
import logging
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from difflib import SequenceMatcher

# Carregar variáveis de ambiente
from dotenv import load_dotenv

# Detectar se está rodando dentro de uma pasta de sessão
current_dir = Path(__file__).parent
session_env = current_dir / '.env'

# Se não tiver .env no diretório atual, verificar pasta sessions/session1
if not session_env.exists():
    session_env = current_dir / 'sessions' / 'session1' / '.env'
    log_dir = current_dir / 'sessions' / 'session1'
else:
    log_dir = current_dir

load_dotenv(session_env)

# Nome da sessão Telegram (usado no arquivo .session)
SESSION_NAME = os.environ.get('SESSION_NAME', 'scanner')

from telethon import TelegramClient
from telethon.tl.types import Message

# ============================================================
# CONFIGURAÇÃO
# ============================================================

API_ID = int(os.environ['TG_API_ID'])
API_HASH = os.environ['TG_API_HASH']

SOURCE_CHAT = int(os.environ['SOURCE_CHAT'])

# Helper para converter topic ID
def _parse_topic(val):
    if not val or val.strip() == '':
        return None
    else:
        return int(val)

SOURCE_TOPIC = _parse_topic(os.environ.get('SOURCE_TOPIC'))

# Arquivo de saída (na mesma pasta da sessão)
OUTPUT_FILE = str(log_dir / 'scan_media_by_user.json')
REPORT_FILE = str(log_dir / 'scan_report.txt')

# Checkpoint (na mesma pasta da sessão)
CHECKPOINT_FILE = str(log_dir / 'scan_checkpoint.txt')

# Sessão Telegram (na mesma pasta da sessão)
SESSION_FILE = str(log_dir / SESSION_NAME)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('scan.log')
    ]
)
log = logging.getLogger(__name__)


# ============================================================
# USERNAME EXTRACTOR
# ============================================================

class UsernameExtractor:
    """
    Extrai usernames de legendas usando múltiplos padrões.
    Usa normalização e similaridade para agrupar usuários similares.
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

    # Sufixos comuns que podem ser removidos
    COMMON_SUFFIXES = [
        'of', 'onlyfans', 'only', 'fan', 'fans',
        'official', 'oficial', 'real', 'real_',
        'vip', 'premium', 'exclusive',
        '_of', '_oficial', '_official', '_real',
        '-of', '-oficial', '-official', '-real'
    ]

    def __init__(self, similarity_threshold: float = 0.85):
        """
        Inicializa extrator de usernames.
        
        Args:
            similarity_threshold: Limiar de similaridade (0.0 a 1.0) para agrupar usernames
        """
        self.similarity_threshold = similarity_threshold
        # Mapeamento: username_canonico -> username_original (primeiro encontrado)
        self.canonical_map: Dict[str, str] = {}
        # Contador de ocorrências para resolver conflitos
        self.username_counts: Dict[str, int] = defaultdict(int)
        # Estatísticas de agrupamento
        self.grouped_count = 0  # Quantos usernames foram agrupados em outros
        self.total_unique_variants = 0  # Total de variações encontradas

    def normalize_username(self, username: str) -> str:
        """
        Normaliza um username removendo sufixos comuns e caracteres extras.
        """
        if not username:
            return username

        # Converter para lowercase
        normalized = username.lower().strip()

        # Remover sufixos comuns
        for suffix in self.COMMON_SUFFIXES:
            # Tenta remover do final
            if normalized.endswith(suffix):
                normalized = normalized[:-len(suffix)].strip('_-')
                break

        # Remover números no final (ex: username123)
        normalized = re.sub(r'\d+$', '', normalized).strip('_-')

        # Remover underscores/traços múltiplos
        normalized = re.sub(r'[-_]{2,}', '_', normalized)

        # Remover underscores/traços no início/fim
        normalized = normalized.strip('_-')

        return normalized if normalized else username

    def calculate_similarity(self, s1: str, s2: str) -> float:
        """
        Calcula similaridade entre duas strings usando SequenceMatcher.
        Retorna valor entre 0.0 (diferente) e 1.0 (igual).
        """
        return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()

    def find_similar_username(self, username: str) -> Optional[str]:
        """
        Busca um username similar já registrado.
        
        Args:
            username: Username para buscar similar
            
        Returns:
            Username canônico se encontrado similar, None caso contrário
        """
        if not self.canonical_map:
            return None

        normalized = self.normalize_username(username)

        # Primeiro, tenta match exato normalizado
        for canonical in self.canonical_map.keys():
            if self.normalize_username(canonical) == normalized:
                return canonical

        # Se não encontrou, busca por similaridade
        # Prioriza usernames com mais ocorrências
        sorted_usernames = sorted(
            self.canonical_map.items(),
            key=lambda x: self.username_counts[x[0]],
            reverse=True
        )

        best_match = None
        best_score = 0.0

        for canonical, _ in sorted_usernames:
            score = self.calculate_similarity(username, canonical)
            
            if score >= self.similarity_threshold and score > best_score:
                best_match = canonical
                best_score = score

        return best_match

    def extract_username(self, caption: str) -> Optional[str]:
        """
        Extrai username de uma legenda.
        Usa similaridade para agrupar usernames parecidos.
        Mantém o username original (primeiro encontrado) no resultado.
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
                    username = username.lower()
                    
                    # Busca username similar já registrado
                    canonical = self.find_similar_username(username)
                    
                    if canonical:
                        # Username similar encontrado, agrupa no canônico
                        # Mas mantém o nome ORIGINAL (primeiro encontrado)
                        self.username_counts[canonical] += 1
                        self.grouped_count += 1
                        if self.grouped_count % 10 == 0:
                            log.debug(f"Agrupado '{username}' em '{canonical}' (total agrupados: {self.grouped_count})")
                        return canonical
                    else:
                        # Novo username, registra como canônico
                        # Mantém este como o nome "oficial" deste usuário
                        self.canonical_map[username] = username  # Mapeia para si mesmo
                        self.username_counts[username] += 1
                        self.total_unique_variants += 1
                        return username

        return None

    def get_user_mapping(self) -> Dict[str, str]:
        """
        Retorna estatísticas de agrupamento de usernames.
        Útil para entender quais usernames foram agrupados.
        
        Returns:
            Dict com usernames que foram agrupados em outros
        """
        # Mapeia usernames que NÃO são canônicos para seus canônicos
        mapping = {}
        for canonical, original in self.canonical_map.items():
            if canonical != original:
                mapping[canonical] = original
        return mapping

    def get_grouping_stats(self) -> Dict[str, any]:
        """
        Retorna estatísticas detalhadas do agrupamento.
        
        Returns:
            Dict com contagem de usernames agrupados por canônico
        """
        grouped_users = defaultdict(list)
        for canonical, original in self.canonical_map.items():
            if canonical != original:
                grouped_users[original].append(canonical)
        
        # Converte para formato legível
        result = {}
        for original, variants in grouped_users.items():
            result[original] = {
                'variants': variants,
                'count': len(variants),
                'total_media': self.username_counts.get(original, 0)
            }
        return result


# ============================================================
# MEDIA SCANNER
# ============================================================

class MediaScanner:
    """
    Scanner que conta mídias por usuário.
    """

    def __init__(self, client: TelegramClient):
        self.client = client
        self.username_extractor = UsernameExtractor()
        
        # Estatísticas por usuário
        self.user_stats: Dict[str, Dict] = defaultdict(lambda: {
            'total_media': 0,
            'videos': 0,
            'photos': 0,
            'documents': 0,
            'albums': 0,
            'total_bytes': 0,
            'first_seen': None,
            'last_seen': None,
            'message_ids': [],  # IDs das mensagens deste usuário
            'album_grouped_ids': set()  # grouped_ids dos álbuns deste usuário
        })
        
        # Contadores gerais
        self.total_messages = 0
        self.total_media = 0
        self.total_albums = 0
        self.no_username = 0  # Mídias sem username identificado
        
        # Para detecção de álbuns
        self.current_album: List[Message] = []
        self.current_grouped_id: int | None = None

    def _get_file_size(self, msg: Message) -> int:
        """Retorna tamanho do arquivo em bytes."""
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

    def _process_media(self, msg: Message, is_album=False, is_album_entry=False, album_id=None):
        """Processa uma mensagem com mídia e atualiza estatísticas."""
        
        # Extrair username
        caption = getattr(msg, 'text', '') or getattr(msg, 'message', '') or ''
        username = self.username_extractor.extract_username(caption)
        
        # Contar tipo de mídia
        is_video = msg.video is not None
        is_photo = msg.photo is not None
        is_document = msg.document is not None
        
        file_size = self._get_file_size(msg)
        
        # Atualizar estatísticas
        if username:
            stats = self.user_stats[username]
            stats['total_media'] += 1
            stats['total_bytes'] += file_size
            
            if is_video:
                stats['videos'] += 1
            elif is_photo:
                stats['photos'] += 1
            elif is_document:
                stats['documents'] += 1
            
            if is_album and is_album_entry:
                # Apenas conta o álbum uma vez (na primeira mídia do álbum)
                album_ids = stats.setdefault('album_ids', set())
                if album_id not in album_ids:
                    stats['albums'] += 1
                    album_ids.add(album_id)
            
            # Timestamps
            msg_date = msg.date.isoformat() if msg.date else None
            if stats['first_seen'] is None or msg_date < stats['first_seen']:
                stats['first_seen'] = msg_date
            if stats['last_seen'] is None or msg_date > stats['last_seen']:
                stats['last_seen'] = msg_date
                
        else:
            self.no_username += 1
        
        self.total_media += 1

    def _process_media_with_username(self, msg: Message, username: str | None, is_album=False, is_album_entry=False, album_id=None):
        """
        Processa uma mensagem com mídia usando um username específico (para álbuns).
        Usa o mesmo username para todas as mídias do álbum.
        """

        # Contar tipo de mídia
        is_video = msg.video is not None
        is_photo = msg.photo is not None
        is_document = msg.document is not None

        file_size = self._get_file_size(msg)

        # Atualizar estatísticas
        if username:
            stats = self.user_stats[username]
            stats['total_media'] += 1
            stats['total_bytes'] += file_size

            if is_video:
                stats['videos'] += 1
            elif is_photo:
                stats['photos'] += 1
            elif is_document:
                stats['documents'] += 1

            if is_album and is_album_entry:
                # Apenas conta o álbum uma vez (na primeira mídia do álbum)
                album_ids = stats.setdefault('album_ids', set())
                if album_id not in album_ids:
                    stats['albums'] += 1
                    album_ids.add(album_id)
                    # Salvar grouped_id do álbum
                    stats['album_grouped_ids'].add(album_id)

            # Salvar ID da mensagem
            stats['message_ids'].append(msg.id)

            # Timestamps
            msg_date = msg.date.isoformat() if msg.date else None
            if stats['first_seen'] is None or msg_date < stats['first_seen']:
                stats['first_seen'] = msg_date
            if stats['last_seen'] is None or msg_date > stats['last_seen']:
                stats['last_seen'] = msg_date

        else:
            self.no_username += 1

        self.total_media += 1

    async def scan(self):
        """Executa o scan de todas as mensagens."""
        
        log.info("=" * 60)
        log.info("SCAN DE MÍDIAS POR USUÁRIO")
        log.info("=" * 60)
        log.info(f"Origem: {SOURCE_CHAT} (tópico: {SOURCE_TOPIC})")
        log.info("=" * 60)
        
        # Carregar checkpoint
        last_id = 0
        if os.path.exists(CHECKPOINT_FILE):
            with open(CHECKPOINT_FILE) as f:
                last_id = int(f.read().strip())
            log.info(f"Resumindo de msg {last_id}")
        
        start_time = time.time()
        
        async def flush_album():
            """Processa o álbum atual."""
            if not self.current_album:
                return

            # Se tem grouped_id, é um álbum (mesmo que tenha apenas 1 mensagem)
            if self.current_grouped_id is not None:
                # Álbum - primeiro, encontrar o username "representante" do álbum
                # Busca o username da primeira mensagem que tiver um
                album_username = None
                for msg in self.current_album:
                    caption = getattr(msg, 'text', '') or getattr(msg, 'message', '') or ''
                    album_username = self.username_extractor.extract_username(caption)
                    if album_username:
                        break

                # Agora processa TODAS as mídias usando o mesmo username
                for i, msg in enumerate(self.current_album):
                    is_first = (i == 0)
                    self._process_media_with_username(
                        msg,
                        username=album_username,
                        is_album=True,
                        is_album_entry=is_first,
                        album_id=self.current_grouped_id
                    )
                    if is_first:
                        self.total_albums += 1
                    self.save_checkpoint(msg.id)
            else:
                # Mensagem individual (sem grouped_id)
                msg = self.current_album[0]
                self._process_media(msg, is_album=False)
                self.save_checkpoint(msg.id)

            self.current_album = []
            self.current_grouped_id = None

        # Iterar mensagens
        async for msg in self.client.iter_messages(
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
            
            self.total_messages += 1
            
            # Verificar se tem mídia
            if not (msg.video or msg.photo or msg.document or msg.audio):
                continue
            
            # Verificar se é parte de um álbum
            grouped_id = getattr(msg, 'grouped_id', None)
            
            if grouped_id is None:
                # Mensagem individual - processar álbum anterior primeiro
                await flush_album()
                self.current_album = [msg]
                self.current_grouped_id = None
                await flush_album()
                
            elif grouped_id == self.current_grouped_id:
                # Mesmo álbum
                self.current_album.append(msg)
                
            else:
                # Novo álbum
                await flush_album()
                self.current_album = [msg]
                self.current_grouped_id = grouped_id
            
            # Log de progresso
            if self.total_messages > 0 and self.total_messages % 1000 == 0:
                elapsed = time.time() - start_time
                rate = self.total_messages / elapsed if elapsed > 0 else 0
                unique_users = len(self.user_stats)
                log.info(
                    f"Progresso: {self.total_messages} msgs | "
                    f"{self.total_media} mídias | "
                    f"{unique_users} usuários | "
                    f"{rate:.0f} msg/s"
                )
        
        # Processar último álbum pendente
        await flush_album()
        
        elapsed = time.time() - start_time
        log.info(f"Scan completo em {elapsed:.1f}s")
        log.info(f"Total mensagens: {self.total_messages}")
        log.info(f"Total mídias: {self.total_media}")
        log.info(f"Total álbuns: {self.total_albums}")
        log.info(f"Usuários únicos: {len(self.user_stats)}")
        log.info(f"Mídias sem username: {self.no_username}")
        
        # Log de agrupamento
        if self.username_extractor.grouped_count > 0:
            log.info(f"🔀 Usernames agrupados: {self.username_extractor.grouped_count}")
            log.info(f"   Variações encontradas: {self.username_extractor.total_unique_variants}")

    def save_checkpoint(self, msg_id: int):
        """Salva checkpoint."""
        try:
            with open(CHECKPOINT_FILE, 'w') as f:
                f.write(str(msg_id))
        except Exception as e:
            log.warning(f"Erro ao salvar checkpoint: {e}")

    def save_results(self):
        """Salva resultados em JSON."""

        # Remover campo temporário album_ids e converter sets para listas
        clean_stats = {}
        for username, stats in self.user_stats.items():
            clean_stats[username] = {
                k: (list(v) if isinstance(v, set) else v)
                for k, v in stats.items()
                if k != 'album_ids'
            }

        # Salvar JSON completo
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'scan_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'source_chat': SOURCE_CHAT,
                'total_users': len(self.user_stats),
                'total_media': self.total_media,
                'total_albums': self.total_albums,
                'no_username': self.no_username,
                'grouping_stats': {
                    'grouped_count': self.username_extractor.grouped_count,
                    'total_unique_variants': self.username_extractor.total_unique_variants
                },
                'users': clean_stats
            }, f, indent=2, ensure_ascii=False)

        log.info(f"💾 Resultados salvos em: {OUTPUT_FILE}")

    def generate_report(self):
        """Gera relatório em texto."""
        
        # Ordenar por total de mídias (decrescente)
        sorted_users = sorted(
            self.user_stats.items(),
            key=lambda x: x[1]['total_media'],
            reverse=True
        )
        
        lines = []
        lines.append("=" * 80)
        lines.append("RELATÓRIO DE SCAN - MÍDIAS POR USUÁRIO")
        lines.append("=" * 80)
        lines.append(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Origem: {SOURCE_CHAT}")
        lines.append("")
        lines.append(f"Total de mensagens processadas: {self.total_messages}")
        lines.append(f"Total de mídias encontradas: {self.total_media}")
        lines.append(f"Total de álbuns: {self.total_albums}")
        lines.append(f"Usuários únicos identificados: {len(self.user_stats)}")
        lines.append(f"Mídias sem username identificado: {self.no_username}")
        
        # Estatísticas de agrupamento
        if self.username_extractor.grouped_count > 0:
            lines.append("")
            lines.append("🔀 Estatísticas de Agrupamento:")
            lines.append(f"   Usernames agrupados em usuários existentes: {self.username_extractor.grouped_count}")
            lines.append(f"   Total de variações processadas: {self.username_extractor.total_unique_variants}")
        
        lines.append("")
        lines.append("=" * 80)
        lines.append(f"TODOS OS {len(sorted_users)} USUÁRIOS POR QUANTIDADE DE MÍDIAS")
        lines.append("=" * 80)
        lines.append("")
        lines.append(f"{'#':<4} {'Usuário':<30} {'Mídia':>7} {'Vídeo':>7} {'Foto':>6} {'Doc':>5} {'Álbuns':>7} {'Tamanho':>12}")
        lines.append("-" * 80)
        
        for i, (username, stats) in enumerate(sorted_users, 1):
            total_mb = stats['total_bytes'] / (1024 * 1024)
            size_str = f"{total_mb:.1f} MB" if total_mb < 1024 else f"{total_mb/1024:.2f} GB"
            
            lines.append(
                f"{i:<4} {username:<30} "
                f"{stats['total_media']:>7} "
                f"{stats['videos']:>7} "
                f"{stats['photos']:>6} "
                f"{stats['documents']:>5} "
                f"{stats['albums']:>7} "
                f"{size_str:>12}"
            )
        
        sorted_by_size = sorted(
            self.user_stats.items(),
            key=lambda x: x[1]['total_bytes'],
            reverse=True
        )
        
        lines.append("")
        lines.append("=" * 80)
        lines.append(f"TODOS OS {len(sorted_by_size)} USUÁRIOS POR TAMANHO TOTAL")
        lines.append("=" * 80)
        lines.append("")
        
        for i, (username, stats) in enumerate(sorted_by_size, 1):
            total_gb = stats['total_bytes'] / (1024**3)
            lines.append(f"{i}. {username:<30} {total_gb:.2f} GB ({stats['total_media']} mídias)")
        
        lines.append("")
        lines.append("=" * 80)
        
        # Salvar relatório
        with open(REPORT_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        
        log.info(f"📊 Relatório salvo em: {REPORT_FILE}")
        
        # Também imprimir no console
        print("\n" + "\n".join(lines))


# ============================================================
# MAIN
# ============================================================

async def main():
    # Usar sessão existente
    log.info(f"Usando sessão: {SESSION_FILE}")
    
    async with TelegramClient(SESSION_FILE, API_ID, API_HASH) as client:
        scanner = MediaScanner(client)
        
        await scanner.scan()
        scanner.save_results()
        scanner.generate_report()
        
        log.info("=" * 60)
        log.info("✅ SCAN CONCLUÍDO!")
        log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
