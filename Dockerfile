ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIND_HOST=0.0.0.0 PORT=49152
COPY engine.py server.py workflow.py settings.py index.html workflow.js ./
RUN install -d -m 0700 -o 65534 -g 65534 /app/data
VOLUME /app/data
USER 65534:65534
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import os, urllib.request; port = int(os.environ['PORT']); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).read()"]
CMD ["python", "server.py"]