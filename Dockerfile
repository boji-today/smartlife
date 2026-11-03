FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir tinytuya==1.20.0

COPY collect.py .

CMD ["python", "-u", "collect.py"]
