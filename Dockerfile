# Use official lightweight Python 3.11 image
FROM python:3.12.10-slim

# Set working directory inside the container
WORKDIR /app

# Copy requirements first — Docker caches this layer
# so reinstalling packages is skipped if requirements haven't changed
COPY requirements.txt .

# Install all Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all remaining project files into the container
COPY . .

# Cloud Run Always Free tier uses port 8080
EXPOSE 8080

# Start FastAPI with uvicorn on port 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]