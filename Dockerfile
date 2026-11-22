# Risk Miner – aplikacja WWW (python -m riskminer serve).
# Narzędzia do dokumentów: pdftotext/pdftoppm (poppler), tesseract z językiem polskim (OCR skanów),
# pypdf + openpyxl + xlrd (PDF, XLSX, XLS). LibreOffice (.doc/.odt/.rtf) jest duży – włącz: --build-arg LIBREOFFICE=1
FROM python:3.12-slim
ARG LIBREOFFICE=0
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-pol \
 && if [ "$LIBREOFFICE" = "1" ]; then apt-get install -y --no-install-recommends libreoffice-writer-nogui libreoffice-calc-nogui; fi \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "pypdf>=4" "openpyxl>=3" "xlrd>=2"
WORKDIR /app
COPY riskminer/ riskminer/
COPY web/ web/
COPY config.example.toml pyproject.toml README.md ./
ENV PYTHONUNBUFFERED=1 \
    RISKMINER_SERVER_HOST=0.0.0.0 \
    RISKMINER_SERVER_OPEN_BROWSER=false
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)" || exit 1
CMD ["python", "-m", "riskminer", "serve"]
