# Module Home Assistant (add-on) : Maison OS.
# Le Supervisor construit cette image sur le Pi à partir de ce dossier.
ARG BUILD_FROM
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY maison ./maison
RUN pip install . && python -m maison --help > /dev/null

EXPOSE 8790
CMD ["python", "-m", "maison", "serve", "--data", "/data"]
