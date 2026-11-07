FROM python:3.12-alpine
RUN pip install --no-cache-dir "psycopg[binary]==3.2.3" \
 && mkdir -p /data/uploads /app \
 && adduser -D -u 10001 app \
 && chown app /data/uploads
COPY app.py /app/app.py
USER 10001
EXPOSE 8080
CMD ["python", "-u", "/app/app.py"]
