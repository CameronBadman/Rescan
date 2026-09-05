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
