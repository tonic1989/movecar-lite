FROM docker.m.daocloud.io/library/python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data \
    PORT=8000
WORKDIR /app
COPY app.py ./
COPY static ./static
COPY vendor ./vendor
RUN mkdir -p /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8000,timeout=2); c.request('GET','/admin'); sys.exit(0 if c.getresponse().status==200 else 1)"
CMD ["python", "app.py"]
