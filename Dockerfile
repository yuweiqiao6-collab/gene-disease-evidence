FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RAG_RUNTIME_DIR=/app/.runtime
WORKDIR /app
COPY requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt && useradd --uid 10001 --create-home appuser
COPY --chown=appuser:appuser src ./src
COPY --chown=appuser:appuser examples ./examples
COPY --chown=appuser:appuser .streamlit/config.toml ./.streamlit/config.toml
RUN mkdir /app/.runtime && chown appuser:appuser /app/.runtime && chmod 700 /app/.runtime
USER appuser
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health',timeout=3)"
CMD ["python", "-m", "streamlit", "run", "src/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
