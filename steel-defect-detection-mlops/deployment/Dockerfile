# Steel Defect Detection - Production Docker Image
# Multi-stage build for optimized image size

FROM python:3.10-slim as base

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir \
    ultralytics \
    fastapi \
    uvicorn \
    python-multipart \
    pillow \
    numpy \
    pydantic

# Copy application code
COPY src/ /app/src/
COPY configs/ /app/configs/

# Copy trained model
COPY runs/detect/steel_defect_colab_50_epochs/weights/best.pt /app/models/best.pt

# Create API script
COPY deployment/api.py /app/api.py

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Run the API server
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
