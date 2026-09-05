package dev.rescan.common;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.context.annotation.*;

@Configuration
public class Infrastructure {
  @Bean public ObjectMapper json() {
    return new ObjectMapper().findAndRegisterModules().disable(com.fasterxml.jackson.databind.SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
  }
  @Bean public TursoDb database() {
    return new TursoDb(Settings.require("TURSO_DATABASE_URL"), ()->Secrets.get("TURSO_AUTH_TOKEN","TURSO_SECRET_ARN"));
  }
  @Bean public JobStore jobs(TursoDb db) { return new JobStore(db); }
  public static void migrate(TursoDb db) { db.migrate(); }
}
