#!/usr/bin/env python3
"""
Helper para descobrir IDs de chats/grupos/canais
"""
import asyncio
import os
from telethon import TelegramClient

async def main():
    # Você só precisa ter API_ID e API_HASH configurados
    api_id = os.environ.get('TG_API_ID')
    api_hash = os.environ.get('TG_API_HASH')
    
    if not api_id or not api_hash:
        print("❌ Configure TG_API_ID e TG_API_HASH no .env primeiro!")
        return
    
    client = TelegramClient('session', int(api_id), api_hash)
    
    await client.start()
    print("\n📋 Seus chats/grupos/canais:\n")
    
    async for dialog in client.iter_dialogs():
        # Mostrar apenas grupos e canais (não DMs)
        if dialog.is_group or dialog.is_channel:
            print(f"Nome: {dialog.name}")
            print(f"ID: {dialog.id}")
            print(f"Tipo: {'Grupo' if dialog.is_group else 'Canal'}")
            print("-" * 50)
    
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
