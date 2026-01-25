#!/usr/bin/env python3
"""
Script para formatar e exibir os resultados do scan de usuários.
Lê o arquivo scan_media_by_user.json e apresenta os dados de forma legível.
"""

import json
from pathlib import Path

# Arquivo de resultados
RESULTS_FILE = Path(__file__).parent / 'scan_media_by_user.json'

def format_bytes(bytes_value):
    """Formata bytes para KB, MB ou GB."""
    if bytes_value == 0:
        return "0 B"
    
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(bytes_value) < 1024.0:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.2f} PB"

def display_results():
    """Lê e exibe os resultados do scan."""
    
    if not RESULTS_FILE.exists():
        print("❌ Arquivo de resultados não encontrado!")
        print(f"   Execute primeiro: python scan_users.py")
        return
    
    with open(RESULTS_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print("\n" + "=" * 100)
    print("RELATÓRIO DE SCAN DE USUÁRIOS ONLYFANS")
    print("=" * 100)
    print(f"📅 Data do Scan: {data['scan_date']}")
    print(f"💬 Chat ID: {data['source_chat']}")
    print()
    print(f"📊 Estatísticas Gerais:")
    print(f"   • Total de usuários identificados: {data['total_users']}")
    print(f"   • Total de mídias: {data['total_media']}")
    print(f"   • Total de álbuns: {data['total_albums']}")
    print(f"   • Mídias sem username: {data['no_username']}")
    
    # Estatísticas de agrupamento
    if data['grouping_stats']['grouped_count'] > 0:
        print()
        print(f"🔀 Estatísticas de Agrupamento:")
        print(f"   • Usernames agrupados: {data['grouping_stats']['grouped_count']}")
        print(f"   • Variações processadas: {data['grouping_stats']['total_unique_variants']}")
    
    users = data.get('users', {})
    
    if not users:
        print()
        print("⚠️  Nenhum usuário encontrado nos resultados.")
        print("   O scan pode não ter sido executado ainda ou não encontrou mídias.")
        print()
        print("=" * 100)
        return
    
    # Ordenar por quantidade de mídias
    sorted_by_media = sorted(
        users.items(),
        key=lambda x: x[1]['total_media'],
        reverse=True
    )
    
    # Ordenar por tamanho
    sorted_by_size = sorted(
        users.items(),
        key=lambda x: x[1]['total_bytes'],
        reverse=True
    )
    
    print()
    print("=" * 100)
    print(f"📋 TODOS OS {len(sorted_by_media)} USUÁRIOS POR QUANTIDADE DE MÍDIAS")
    print("=" * 100)
    print()
    print(f"{'#':<5} {'Usuário':<35} {'Total':>7} {'Vídeo':>7} {'Foto':>7} {'Doc':>6} {'Álbuns':>7} {'Tamanho':>12}")
    print("-" * 100)
    
    for i, (username, stats) in enumerate(sorted_by_media, 1):
        size_str = format_bytes(stats['total_bytes'])
        print(
            f"{i:<5} {username:<35} "
            f"{stats['total_media']:>7} "
            f"{stats['videos']:>7} "
            f"{stats['photos']:>7} "
            f"{stats['documents']:>6} "
            f"{stats['albums']:>7} "
            f"{size_str:>12}"
        )
    
    print()
    print("=" * 100)
    print(f"💾 TODOS OS {len(sorted_by_size)} USUÁRIOS POR TAMANHO TOTAL")
    print("=" * 100)
    print()
    
    for i, (username, stats) in enumerate(sorted_by_size, 1):
        size_str = format_bytes(stats['total_bytes'])
        print(f"{i:3}. {username:<35} {size_str:>12} ({stats['total_media']} mídias)")
    
    print()
    print("=" * 100)
    print("📈 Resumo dos Top 5 Usuários")
    print("=" * 100)
    print()
    
    # Top 5 por quantidade
    print("🥇 Top 5 por quantidade de mídias:")
    for i, (username, stats) in enumerate(sorted_by_media[:5], 1):
        size_str = format_bytes(stats['total_bytes'])
        print(f"   {i}. {username}: {stats['total_media']} mídias ({size_str})")
    
    print()
    print("💎 Top 5 por tamanho total:")
    for i, (username, stats) in enumerate(sorted_by_size[:5], 1):
        size_str = format_bytes(stats['total_bytes'])
        print(f"   {i}. {username}: {size_str} ({stats['total_media']} mídias)")
    
    print()
    print("=" * 100)
    print()

if __name__ == "__main__":
    display_results()
