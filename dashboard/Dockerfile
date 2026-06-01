FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static ./static
EXPOSE 8501
# 單一 worker 即可（Pi 省 RAM）；對 SQLite 是唯讀，併發讀沒問題
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8501", "--workers", "1"]
