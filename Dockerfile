FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y ffmpeg tesseract-ocr tesseract-ocr-eng
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m spacy download en_core_web_md
COPY . .
CMD ["python", "-m", "main"]
