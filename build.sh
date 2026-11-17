#!/usr/bin/env bash
# Instalar FFmpeg (para audios) y Tesseract (para fotos) en Render
apt-get update && apt-get install -y ffmpeg tesseract-ocr tesseract-ocr-spa
pip install -r requirements.txt