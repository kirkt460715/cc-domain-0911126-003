from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json
from urllib.parse import urlsplit
from registry.values import canonical
from bed_events.ledger import RevisionConflict, IntegrityFailure
from audit.service import Service


MAX_BODY = 10_000_000


def make_server(path, host='127.0.0.1', port=0):
    service = Service(path)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            return

        def send_json(self, status, value):
            content = canonical(value).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(content)
            self.close_connection = True

        def request_data(self):
            if self.headers.get('Transfer-Encoding') is not None:
                raise ValueError('chunked requests are unsupported')
            value = self.headers.get('Content-Length', '0')
            if not value.isascii() or not value.isdigit():
                raise ValueError('invalid Content-Length')
            length = int(value)
            if length > MAX_BODY:
                raise OverflowError('request body too large')
            if length == 0:
                return {}
            content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/json':
                raise ValueError('request must use application/json')
            content = self.rfile.read(length)
            if len(content) != length:
                raise ValueError('incomplete body')
            return json.loads(content.decode('utf-8'))

        def serve(self):
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                self.send_json(400, {'error': 'query parameters unsupported; use JSON fields'})
                return
            try:
                data = self.request_data()
                result = service.handle(self.command, parsed.path, data)
            except OverflowError as exc:
                self.send_json(413, {'error': str(exc)})
            except RevisionConflict as exc:
                self.send_json(409, {'error': str(exc)})
            except IntegrityFailure as exc:
                self.send_json(409, {'error': str(exc)})
            except KeyError as exc:
                self.send_json(404, {'error': str(exc)})
            except LookupError as exc:
                self.send_json(404, {'error': str(exc)})
            except (ValueError, UnicodeError, TypeError) as exc:
                self.send_json(400, {'error': str(exc)})
            else:
                self.send_json(200, result)

        def do_GET(self):
            self.serve()

        def do_POST(self):
            self.serve()

        def do_PUT(self):
            self.send_json(405, {'error': 'method not allowed'})

        def do_DELETE(self):
            self.send_json(405, {'error': 'method not allowed'})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
