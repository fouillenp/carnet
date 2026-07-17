FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static ./static
ENV DB_PATH=/data/carnet.db
EXPOSE 80
CMD ["gunicorn", "-b", "0.0.0.0:80", "-w", "2", "app:app"]
