# ---------------------------------------------------------------------------
# EMSO Validation Gate - the one-shot job container
#
# Optional. ./gate.py runs fine on the host with `pip install -r
# requirements.txt`; this image exists for hosts that do not have the compiled
# netCDF/UDUNITS libraries the compliance engine needs, and for CI runners that
# should not grow a Python environment.
#
#     docker compose build gate
#     docker compose run --rm gate check
#     docker compose run --rm gate run new
#
# The compliance engine pulls in cfunits (UDUNITS-2) and cfchecker
# (libxml2/libxslt), so this cannot be a pure python-slim image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libudunits2-0 \
        libudunits2-dev \
        udunits-bin \
        libxml2-dev \
        libxslt1-dev \
        libhdf5-dev \
        libnetcdf-dev \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gate.py ./
COPY validation_gate/ ./validation_gate/
COPY database/sql/ ./database/sql/
COPY erddap/templates/ ./erddap/templates/

# Baked in so the image can validate a registry with nothing else mounted:
#
#   docker run --rm -v "$PWD/federation:/app/federation:ro" <image> check
#
# which is what a facility's own pre-commit check runs. For real runs the
# working tree is bind-mounted over /app and shadows these.
COPY validation-gate.yaml ./
COPY federation/ ./federation/

ENTRYPOINT ["python3", "/app/gate.py"]
CMD ["--help"]
