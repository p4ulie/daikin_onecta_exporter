FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY daikin_onecta_exporter/ ./daikin_onecta_exporter/
# The token file's directory is the only place the process writes; the chart mounts a volume there.
RUN mkdir /data && chown 65534 /data
USER 65534
# ponytail: bytecode writes would fail on the read-only root filesystem, so skip them.
ENV DAIKIN_EXPORTER_PORT=9879 DAIKIN_TOKEN_FILE=/data/token.json PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 9879
CMD ["python", "-m", "daikin_onecta_exporter.exporter"]
