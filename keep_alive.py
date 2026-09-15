from http.server import BaseHTTPRequestHandler, HTTPServer
import os

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Respond with 200 OK for UptimeRobot / Render Health Checks
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Hermes Agent is Alive and Running!")

# Render provides the PORT environment variable
port = int(os.environ.get("PORT", 10000))
httpd = HTTPServer(('0.0.0.0', port), PingHandler)

print(f"Ping server running on port {port}...")
httpd.serve_forever()
