#!/usr/bin/env python3
"""
Script para ler e exibir o relatório de scan existente.
Processa o arquivo scan_report.txt e apresenta os dados de forma formatada.
"""

from pathlib import Path

# Arquivo de relatório
REPORT_FILE = Path(__file__).parent / 'scan_report.txt'

def display_report():
    """Lê e exibe o relatório de scan existente."""
    
    if not REPORT_FILE.exists():
        print("❌ Arquivo de relatório não encontrado!")
        print(f"   Execute primeiro: python scan_users.py")
        return
    
    print(f"\n📄 Lendo relatório de: {REPORT_FILE}")
    print("\n" + "=" * 100)
    
    with open(REPORT_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Imprimir todo o conteúdo do relatório
    print(content)
    
    print()
    print("=" * 100)
    print("ℹ️  INFORMAÇÃO IMPORTANTE:")
    print("=" * 100)
    print()
    print("Este relatório foi gerado com a versão antiga do script scan_users.py")
    print("que mostrava apenas:")
    print("  • Top 50 usuários por quantidade de mídias")
    print("  • Top 10 usuários por tamanho total")
    print()
    print("🔧 Para ver TODOS OS USUÁRIOS (os 441 identificados):")
    print("   O script scan_users.py foi atualizado para mostrar todos os usuários.")
    print("   Execute novamente:")
    print()
    print("   python scan_users.py")
    print()
    print("   Isso irá:")
    print("   1. Usar o checkpoint existente (scan_checkpoint.txt)")
    print("   2. Retomar de onde parou rapidamente")
    print("   3. Gerar um novo relatório completo com TODOS os usuários")
    print("   4. Salvar os dados completos em scan_media_by_user.json")
    print()
    print("💡 Depois disso, você poderá usar:")
    print("   python display_results.py")
    print()
    print("   Para ver os dados formatados sem precisar fazer novo scan.")
    print()
    print("=" * 100)
    print()

if __name__ == "__main__":
    display_report()
