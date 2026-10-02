FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" && useradd --system --uid 10002 sandbox
WORKDIR /srv
COPY sandbox ./sandbox
USER sandbox
EXPOSE 9000
CMD ["uvicorn", "sandbox.app:app", "--host", "0.0.0.0", "--port", "9000"]
