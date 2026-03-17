FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium and ALL its system dependencies automatically
RUN playwright install --with-deps chromium

COPY . .

ENV PORT=5000

CMD python app.py
