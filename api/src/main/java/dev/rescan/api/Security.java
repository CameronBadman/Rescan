package dev.rescan.api;

import dev.rescan.common.Settings;
import java.util.List;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.oauth2.core.*;
import org.springframework.security.oauth2.jwt.*;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.web.cors.*;

@Configuration
public class Security {
  @Bean
  JwtDecoder jwtDecoder() {
    String issuer = Settings.require("AUTH_ISSUER");
    String client = Settings.require("AUTH_CLIENT_ID");
    var decoder =
        NimbusJwtDecoder.withJwkSetUri(
                Settings.get("AUTH_JWKS_URI", issuer + "/.well-known/jwks.json"))
            .build();
    OAuth2TokenValidator<Jwt> clientValidator =
        jwt ->
            client.equals(jwt.getClaimAsString("client_id"))
                    && "access".equals(jwt.getClaimAsString("token_use"))
                    && jwt.getSubject() != null
                ? OAuth2TokenValidatorResult.success()
                : OAuth2TokenValidatorResult.failure(new OAuth2Error("invalid_token"));
    decoder.setJwtValidator(
        new DelegatingOAuth2TokenValidator<>(
            JwtValidators.createDefaultWithIssuer(issuer), clientValidator));
    return decoder;
  }

  @Bean
  SecurityFilterChain filter(HttpSecurity http) throws Exception {
    return http.csrf(c -> c.disable())
        .cors(c -> {})
        .sessionManagement(
            s ->
                s.sessionCreationPolicy(
                    org.springframework.security.config.http.SessionCreationPolicy.STATELESS))
        .authorizeHttpRequests(
            a -> a.requestMatchers("/actuator/health").permitAll().anyRequest().authenticated())
        .oauth2ResourceServer(o -> o.jwt(j -> {}))
        .build();
  }

  @Bean
  CorsConfigurationSource cors() {
    var config = new CorsConfiguration();
    config.setAllowedOrigins(List.of(Settings.require("FRONTEND_ORIGIN")));
    config.setAllowedMethods(List.of("GET", "POST", "DELETE", "OPTIONS"));
    config.setAllowedHeaders(List.of("Authorization", "Content-Type", "Idempotency-Key"));
    var source = new UrlBasedCorsConfigurationSource();
    source.registerCorsConfiguration("/**", config);
    return source;
  }
}
