FROM maven:3.9.11-eclipse-temurin-21 AS build
WORKDIR /src
COPY . .
RUN mvn -B -DskipTests package
FROM eclipse-temurin:21-jre AS api
WORKDIR /app
COPY --from=build /src/api/target/api-0.1.0-SNAPSHOT.jar /app/api.jar
USER 10001
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/api.jar"]
FROM eclipse-temurin:21-jre-jammy AS worker
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv libgl1 libgomp1 libglib2.0-0 fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY worker/python /app/python
RUN python3 -m venv /app/venv && /app/venv/bin/pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r /app/python/requirements.txt && /app/venv/bin/python /app/python/download_models.py
COPY --from=build /src/worker/target/worker-0.1.0-SNAPSHOT.jar /app/worker.jar
ENV PYTHON_BIN=/app/venv/bin/python VIT_SCRIPT=/app/python/recognize.py HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
USER 10001
ENTRYPOINT ["java", "-Xmx512m", "-jar", "/app/worker.jar"]
