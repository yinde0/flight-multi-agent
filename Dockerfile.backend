FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
RUN mkdir -p flight_agent travel_eval && touch flight_agent/__init__.py travel_eval/__init__.py
RUN pip install --no-cache-dir ".[app]"

COPY flight_agent ./flight_agent
COPY travel_eval ./travel_eval

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
