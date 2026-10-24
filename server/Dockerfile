FROM python:3.12-slim
LABEL app=fpv-spotter
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/spotter_api ./spotter_api
ENV SPOTTER_DATA=/data TZ=Europe/London
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "spotter_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
