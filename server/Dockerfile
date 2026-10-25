# The API, with the scoring pipeline alongside it so it can re-rank a region with your ratings.
FROM python:3.12-slim
LABEL app=fpv-spotter
WORKDIR /app
# rasterio's bundled GDAL wants libexpat from the system
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*
COPY server/requirements.txt server-requirements.txt
COPY pyproject.toml /app/repo/pyproject.toml
COPY spotfinder /app/repo/spotfinder
RUN pip install --no-cache-dir -r server-requirements.txt -e /app/repo
COPY config /app/repo/config
COPY server/spotter_api ./spotter_api
# the pipeline's downloads, caches and per-region features live in the data volume
ENV SPOTTER_DATA=/data SPOTFINDER_DATA=/data/pipeline TZ=Europe/London PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "spotter_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
