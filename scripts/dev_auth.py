"""Loopback-only development JWT issuer, backed by ephemeral RSA keys.

This is a local testing utility, never part of the API or production containers.
"""
import base64
import json
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


with tempfile.TemporaryDirectory(prefix="rescan-dev-auth-") as directory:
    key = Path(directory) / "private.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)], check=True, stderr=subprocess.DEVNULL)
    modulus = subprocess.check_output(["openssl", "rsa", "-in", str(key), "-noout", "-modulus"]).decode().strip().split("=", 1)[1]
    jwk = {"kty": "RSA", "use": "sig", "kid": "local", "alg": "RS256", "n": b64(bytes.fromhex(modulus)), "e": "AQAB"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/.well-known/jwks.json":
                response = {"keys": [jwk]}
            elif self.path == "/token":
                now = int(time.time())
                header = b64(json.dumps({"alg": "RS256", "kid": "local"}).encode())
                claims = b64(json.dumps({"iss": "http://localhost:9000", "sub": "local-user", "client_id": "rescan-local", "token_use": "access", "iat": now, "exp": now + 3600}).encode())
                body = f"{header}.{claims}".encode()
                signature = subprocess.check_output(["openssl", "dgst", "-sha256", "-sign", str(key)], input=body)
                response = {"access_token": body.decode() + "." + b64(signature), "expires_in": 3600}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    print("Local token endpoint: http://localhost:9000/token", flush=True)
    HTTPServer(("127.0.0.1", 9000), Handler).serve_forever()
