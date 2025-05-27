# Step 1A: Overwrite Dockerfile
cat > Dockerfile << 'EOF'
FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y build-essential cmake git && \
    rm -rf /var/lib/apt/lists/*

COPY . .

RUN cd llama.cpp && \
    mkdir -p build && \
    cd build && \
    cmake .. && \
    make -j$(nproc)

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
EOF
