# Penn Track Engine -- stdlib only, so the image is just Python plus three files.
FROM python:3.12-slim

WORKDIR /app
COPY engine.py njt_logger.py codebook.json ./

# 0.0.0.0 so the platform's front door can reach the process (127.0.0.1 would
# mean "this container only" and every request would time out).
ENV BIND=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["python", "engine.py"]
