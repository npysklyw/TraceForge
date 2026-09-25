FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e '.[dev]'
COPY tests ./tests
COPY alembic.ini ./
COPY migrations ./migrations
EXPOSE 8000
CMD ["uvicorn", "benchwarden.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
