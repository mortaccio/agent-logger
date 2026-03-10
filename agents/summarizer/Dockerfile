FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN useradd --create-home --uid 10001 agent

COPY app.py /app/app.py
COPY log_tools.py /app/log_tools.py
COPY prompts.py /app/prompts.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && chown -R agent:agent /app

USER agent

ENTRYPOINT ["/app/entrypoint.sh"]
