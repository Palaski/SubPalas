FROM python:3.9-slim

# 1. Instalar dependências do sistema (FFmpeg é obrigatório para a mágica acontecer)
# O 'git' e outros utilitários ajudam a evitar erros em algumas libs
RUN apt-get update && \
    apt-get install -y ffmpeg git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Copiar e instalar as dependências do Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copiar o código do addon
COPY addon.py .

# 4. Criar pastas para cache temporário (importante para não dar erro de permissão)
RUN mkdir -p subtitle_cache temp_processing && \
    chmod 777 subtitle_cache temp_processing

# 5. Expor a porta
EXPOSE 7000

# 6. Comando para rodar o servidor
CMD ["gunicorn", "--bind", "0.0.0.0:7000", "--workers", "4", "--threads", "2", "--timeout", "120", "addon:app"]