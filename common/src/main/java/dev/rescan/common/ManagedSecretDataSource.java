package dev.rescan.common;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import org.springframework.jdbc.datasource.AbstractDataSource;
import software.amazon.awssdk.services.secretsmanager.SecretsManagerClient;

/** Resolve current credentials whenever Hikari opens a new physical connection. */
final class ManagedSecretDataSource extends AbstractDataSource {
  private final String url;
  private final String secretArn;
  private final ObjectMapper json;

  ManagedSecretDataSource(String url, String secretArn, ObjectMapper json) {
    this.url = url;
    this.secretArn = secretArn;
    this.json = json;
  }

  @Override
  public Connection getConnection() throws SQLException {
    try (var secrets = SecretsManagerClient.create()) {
      var credentials =
          json.readTree(secrets.getSecretValue(r -> r.secretId(secretArn)).secretString());
      return DriverManager.getConnection(
          url, credentials.get("username").asText(), credentials.get("password").asText());
    } catch (SQLException error) {
      throw error;
    } catch (Exception error) {
      throw new SQLException("Managed database credentials unavailable", "08001", error);
    }
  }

  @Override
  public Connection getConnection(String username, String password) throws SQLException {
    return getConnection();
  }
}
