package dev.rescan.common;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import javax.sql.DataSource;
import org.flywaydb.core.Flyway;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

@Configuration
public class Infrastructure {
  @Bean
  public ObjectMapper json() {
    return new ObjectMapper()
        .findAndRegisterModules()
        .disable(com.fasterxml.jackson.databind.SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
  }

  @Bean
  public DataSource dataSource(ObjectMapper json) throws Exception {
    var config = new HikariConfig();
    config.setJdbcUrl(Settings.require("DATABASE_URL"));
    config.setUsername(Settings.get("DATABASE_USER", "rescan"));
    String secret = Settings.get("DATABASE_SECRET_ARN", "");
    if (!secret.isEmpty()) {
      config.setDataSource(new ManagedSecretDataSource(config.getJdbcUrl(), secret, json));
      config.setJdbcUrl(null);
    } else config.setPassword(Settings.require("DATABASE_PASSWORD"));
    config.setMaximumPoolSize(Settings.integer("DATABASE_POOL_SIZE", 5));
    return new HikariDataSource(config);
  }

  @Bean
  public JdbcTemplate jdbc(DataSource source) {
    return new JdbcTemplate(source);
  }

  @Bean
  public TransactionTemplate tx(DataSource source) {
    return new TransactionTemplate(new DataSourceTransactionManager(source));
  }

  @Bean
  public JobStore jobs(JdbcTemplate jdbc, TransactionTemplate tx) {
    return new JobStore(jdbc, tx);
  }

  public static void migrate(DataSource source) {
    Flyway.configure().dataSource(source).load().migrate();
  }
}
