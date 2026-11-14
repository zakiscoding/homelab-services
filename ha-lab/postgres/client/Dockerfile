FROM python:3.12-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.2.13"
COPY workload.py /app/workload.py
RUN mkdir /out
WORKDIR /app
