FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /srv

COPY pyproject.toml README.md LICENSE ./
COPY engine ./engine
COPY panel ./panel

RUN pip install --no-cache-dir .

EXPOSE 8787 8000

CMD ["python", "-m", "engine"]