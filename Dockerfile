FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir tinytuya==1.20.0 croniter==6.2.4 websocket-client==1.9.2

COPY collect.py .
COPY smartlife ./smartlife

EXPOSE 8080

CMD ["python", "-u", "collect.py"]
