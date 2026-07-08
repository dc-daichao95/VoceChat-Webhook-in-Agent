FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DATA_DIR=/webhook_share LISTEN_HOST=0.0.0.0 LISTEN_PORT=8091
EXPOSE 8091
CMD ["uvicorn", "app.receiver:create_app", "--factory", "--host", "0.0.0.0", "--port", "8091"]
