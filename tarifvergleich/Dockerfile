FROM python:3.12.7-alpine3.20

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt
COPY requirements.txt /opt/requirements.txt
# Build-Tools nur als Fallback, falls für eine Architektur kein fertiges Wheel existiert
RUN apk add --no-cache --virtual .build-deps gcc musl-dev \
    && pip install -r /opt/requirements.txt \
    && apk del .build-deps

COPY app /opt/app

CMD ["python3", "-m", "app.main"]
