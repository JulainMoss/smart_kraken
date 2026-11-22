# Ekstraktor LLM – analiza opisu inwestycji lokalnym modelem (serwer.py).
# OCR skanów: tesseract z językiem polskim + pdftoppm (poppler). PDF: pypdf[image] (z Pillow).
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-pol poppler-utils \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "pypdf[image]>=4.0"
WORKDIR /app
COPY serwer.py index.html ./
COPY assets/ assets/
ENV PYTHONUNBUFFERED=1
VOLUME ["/app/wyniki", "/app/debug"]
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/status', timeout=4)" || exit 1
ENTRYPOINT ["python", "serwer.py", "--host", "0.0.0.0", "--port", "8001", "--no-browser"]
CMD ["--llm-url", "http://ollama:11434", "--model", "SpeakLeash/bielik-1.5b-v3.0-instruct:Q8_0"]
