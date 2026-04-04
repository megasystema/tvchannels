FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir requests beautifulsoup4 playwright flask

ENV PORT=8087
EXPOSE 8087

CMD ["python", "thetvappproxy.py"]