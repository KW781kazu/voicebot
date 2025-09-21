FROM public.ecr.aws/docker/library/python:3.11-slim

# system deps（できるだけ軽く）
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

# FastAPI起動（ALBからは80番で到達する前提：ECS側でポートマッピング）
ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]

