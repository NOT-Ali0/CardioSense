# 1. Base image: Python 3.11 slim
FROM python:3.11-slim

# 2. Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py

# 3. Work directory inside container
WORKDIR /app

# 4. Create non-root user and pre-create instance directory
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/instance && \
    chown -R appuser:appuser /app

# 5. Install Python dependencies (layer cached)
RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 6. Copy project source code and assign ownership to appuser
COPY . .
RUN chown -R appuser:appuser /app

# 7. Switch to non-root user
USER appuser

# 8. Expose application port
EXPOSE 10000

# 9. Start Flask application via Gunicorn WSGI server (single worker + threads for SQLite & in-memory PyTorch model)
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--workers", "1", "--threads", "4"]
