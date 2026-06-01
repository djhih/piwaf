FROM python:3.12-slim
WORKDIR /app
COPY parser.py .
# 只用標準函式庫（json/sqlite3/re），不需額外套件
CMD ["python", "-u", "parser.py"]
