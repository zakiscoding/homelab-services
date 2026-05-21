FROM python:3.12-slim
WORKDIR /app
COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/rag_engine.py api/rag_store.py ./
COPY worker/poll_imac.py worker/run_ingest.py worker/run_embed.py ./
ENV PYTHONPATH=/app
CMD ["python", "poll_imac.py"]
