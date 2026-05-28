FROM python:3.12-slim
WORKDIR /app
COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/rag_engine.py api/rag_store.py ./
COPY api/train_plan.py ./
COPY worker/poll_imac.py worker/run_ingest.py worker/run_embed.py worker/run_train_prep.py worker/job_wake.py ./
ENV PYTHONPATH=/app
CMD ["python", "poll_imac.py"]
