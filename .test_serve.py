import sys
sys.path.insert(0, '.')
from http.server import ThreadingHTTPServer
from studio import server as S
S.Handler.require_auth = False
S.Handler.allow_origins = frozenset(["http://127.0.0.1:8801"])
print("happy-path test server on 8799", flush=True)
ThreadingHTTPServer(("127.0.0.1", 8799), S.Handler).serve_forever()
