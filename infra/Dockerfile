FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY crawler ./crawler
COPY api ./api
COPY web ./web
RUN pip install --no-cache-dir . fastapi "uvicorn>=0.29.0"
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
