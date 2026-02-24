FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Run as a module so imports like `from src...` work reliably
CMD ["python", "-m", "src.main"]
