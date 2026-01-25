#!/usr/bin/env python3
"""
Gera o logo do meninos.app.
- Fonte geométrica bold (similar a Outfit)
- Texto todo em branco
- Tracking tighter (espaçamento reduzido)
"""

from PIL import Image, ImageDraw, ImageFont
import os

def get_outfit_style_font(font_size: int):
    """Retorna fonte geométrica bold similar a Outfit."""

    # Fontes geométricas bold em ordem de preferência
    font_candidates = [
        # Por nome (fontconfig)
        'Roboto-Bold',
        'Ubuntu-Bold',
        'NotoSans-Bold',
        'LiberationSans-Bold',
        'DejaVuSans-Bold',
        'Arial-Bold',
    ]

    # Tentar por nome primeiro
    for font_name in font_candidates:
        try:
            return ImageFont.truetype(font_name, font_size)
        except:
            continue

    # Tentar por caminho
    font_paths = [
        '/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf',
        '/usr/share/fonts/truetype/roboto/Roboto-Bold.ttf',
        '/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    ]

    for font_path in font_paths:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, font_size)
            except:
                continue

    return ImageFont.load_default()

def create_meninos_logo(font_size: int = 20, output_path: str = 'meninos_logo_small.png'):
    """Cria logo 'meninos.app' em branco."""

    font = get_outfit_style_font(font_size)

    # Texto completo
    text = "meninos.app"

    # Medir tamanho do texto
    temp_img = Image.new('RGBA', (1, 1))
    temp_draw = ImageDraw.Draw(temp_img)
    bbox = temp_draw.textbbox((0, 0), text, font=font)

    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    # Tamanho total com padding mínimo
    padding = 4
    total_width = text_width + padding * 2
    total_height = text_height + padding * 2

    # Criar imagem transparente
    logo = Image.new('RGBA', (total_width, total_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(logo)

    # Posição do texto
    x_pos = padding
    y_pos = padding

    # Desenhar texto em branco puro
    draw.text((x_pos, y_pos), text, font=font, fill=(255, 255, 255, 255))

    # Adicionar sombra sutil para legibilidade
    shadow = Image.new('RGBA', (total_width, total_height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.text((x_pos + 1, y_pos + 1), text, font=font, fill=(0, 0, 0, 80))

    # Combinar sombra + logo
    final = Image.alpha_composite(shadow, logo)

    # Crop para remover espaço vazio
    bbox = final.getbbox()
    if bbox:
        final = final.crop(bbox)

    final.save(output_path, 'PNG')
    print(f"Logo salvo: {output_path} ({final.size[0]}x{final.size[1]})")

    return output_path

if __name__ == '__main__':
    # Versão pequena para watermark
    create_meninos_logo(font_size=18, output_path='meninos_logo_small.png')

    # Versão média
    create_meninos_logo(font_size=24, output_path='meninos_logo_medium.png')
