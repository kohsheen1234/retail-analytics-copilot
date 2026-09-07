FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://ollama.com/install.sh | sh
WORKDIR /work
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV OLLAMA_HOST=0.0.0.0:11434
# Start Ollama, pull the pinned model once, then drop into a shell.
CMD ["bash", "-lc", "ollama serve & sleep 3 && ollama pull phi3.5:3.8b-mini-instruct-q4_K_M && exec bash"]
