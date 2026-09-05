package dev.rescan.api;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication(scanBasePackages = "dev.rescan")
public class ApiApplication {
  public static void main(String[] args) {
    SpringApplication.run(ApiApplication.class, args);
  }
}
