# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DMASIF_STATE_DIR=/var/lib/dmasif-console

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 render \
    && groupadd --gid 10001 console \
    && useradd --uid 10001 --gid console --groups render --create-home --shell /bin/bash console \
    && install -d -o console -g console -m 0700 /var/lib/dmasif-console /var/data /home/console/.ssh

WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY dmasif_console/ ./dmasif_console/
COPY cluster_adapter/ ./cluster_adapter/
COPY deploy/git-askpass.py /usr/local/bin/dmasif-git-askpass
RUN chmod 0555 /usr/local/bin/dmasif-git-askpass \
    && python -m pip install --no-cache-dir --no-deps .

USER console
EXPOSE 10000
ENTRYPOINT ["dmasif-console"]
CMD ["serve"]
