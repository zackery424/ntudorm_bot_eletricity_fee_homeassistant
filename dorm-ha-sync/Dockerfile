FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dorm_captcha.py dorm_client.py ha_sync.py ./

# 用容器自帶的排程器跑：每 30 分鐘執行一次 ha_sync.py。
# 環境變數（DORM_ACCOUNT、DORM_PASSWORD、HA_URL、HA_TOKEN...）
# 建議透過 docker run -e 或 docker-compose 的 env_file 帶入，不要寫進 image 裡。
CMD ["sh", "-c", "while true; do python3 ha_sync.py; sleep 1800; done"]
