FROM docker.io/library/maven:3.9.11-eclipse-temurin-21 AS build
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/root/.m2 mvn -q -DskipTests package
FROM docker.io/library/eclipse-temurin:21-jre AS api
WORKDIR /app
COPY --from=build /src/api/target/api-0.1.0-SNAPSHOT.jar /app/api.jar
USER 10001
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/api.jar"]
FROM api AS api-lambda
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 /lambda-adapter /opt/extensions/lambda-adapter
ENV AWS_LWA_PORT=8080 AWS_LWA_READINESS_CHECK_PATH=/actuator/health AWS_LWA_READINESS_CHECK_HEALTHY_STATUS=200 AWS_LWA_ASYNC_INIT=true
FROM docker.io/library/eclipse-temurin:21-jre AS controller
WORKDIR /app
COPY --from=build /src/controller/target/controller-0.1.0-SNAPSHOT.jar /app/controller.jar
USER 10001
ENTRYPOINT ["java", "-cp", "/app/controller.jar", "dev.rescan.controller.Controller"]
CMD ["--loop"]
FROM docker.io/library/python:3.10-slim AS dev-auth
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
COPY scripts/dev_auth.py /app/dev_auth.py
USER 10001
ENTRYPOINT ["python", "/app/dev_auth.py"]
FROM docker.io/library/eclipse-temurin:21-jre-jammy AS worker
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv libgl1 libgomp1 libglib2.0-0 fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY worker/python/requirements.txt /app/python/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip python3 -m venv /app/venv && /app/venv/bin/pip install --extra-index-url https://download.pytorch.org/whl/cpu -r /app/python/requirements.txt
COPY worker/python/models.json worker/python/download_models.py /app/python/
RUN /app/venv/bin/python /app/python/download_models.py
COPY worker/python /app/python
COPY --from=build /src/worker/target/worker-0.1.0-SNAPSHOT.jar /app/worker.jar
ENV PYTHON_BIN=/app/venv/bin/python VIT_SCRIPT=/app/python/recognize.py HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
USER 10001
ENTRYPOINT ["java", "-Xmx512m", "-jar", "/app/worker.jar"]
