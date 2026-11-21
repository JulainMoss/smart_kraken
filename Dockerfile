# Lekki obraz: wyłącznie biblioteka standardowa Pythona (+ opcjonalnie poppler do PDF i LibreOffice do .doc)
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY riskminer/ riskminer/
COPY web/ web/
COPY config.example.toml pyproject.toml README.md ./
ENV RISKMINER_SERVER_HOST=0.0.0.0 RISKMINER_SERVER_OPEN_BROWSER=false
VOLUME ["/app/data"]
EXPOSE 8000
# baza: zamontuj katalog z data/riskminer.sqlite albo zbuduj: docker run ... python -m riskminer build --jsonl /dane/notices.jsonl
CMD ["python", "-m", "riskminer", "serve"]
