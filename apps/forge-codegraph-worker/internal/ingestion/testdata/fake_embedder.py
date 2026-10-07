"""An OpenAI-compatible /embeddings endpoint for local ingestion tests.

It enforces what the real API does to a request (at most 2,048 inputs, each
at most 8,192 tokens counted with the model's own tokenizer, at most 300,000
tokens per request) and answers with deterministic vectors, so an ingestion
that passes here does not fail on input size against the real provider. It
prints the port it listens on, then one JSON line of totals per request to
stderr.

    python fake_embedder.py [--port 0] [--model text-embedding-3-large]

Requires tiktoken.
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tiktoken

MAX_INPUTS = 2048
MAX_INPUT_TOKENS = 8192
MAX_REQUEST_TOKENS = 300_000


def refuse(handler: BaseHTTPRequestHandler, message: str) -> None:
    body = json.dumps(
        {"error": {"message": message, "type": "invalid_request_error", "param": None, "code": None}}
    ).encode()
    handler.send_response(400)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def serve(port: int, model: str) -> None:
    encoding = tiktoken.encoding_for_model(model)
    totals = {"requests": 0, "inputs": 0, "max_input_tokens": 0, "refused": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - the totals line is the log
            pass

        def do_POST(self):
            if not self.path.endswith("/embeddings"):
                self.send_error(404)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            texts = request.get("input") or []
            totals["requests"] += 1
            if not isinstance(texts, list) or not texts or len(texts) > MAX_INPUTS:
                totals["refused"] += 1
                refuse(self, f"'input' must be 1 to {MAX_INPUTS} items, got {len(texts)}")
                return
            counts = [len(encoding.encode(text, disallowed_special=())) for text in texts]
            totals["inputs"] += len(texts)
            totals["max_input_tokens"] = max(totals["max_input_tokens"], *counts)
            over = [n for n in counts if n > MAX_INPUT_TOKENS]
            if over:
                totals["refused"] += 1
                refuse(
                    self,
                    f"This model's maximum context length is {MAX_INPUT_TOKENS} tokens, "
                    f"however you requested {over[0]} tokens.",
                )
                return
            if sum(counts) > MAX_REQUEST_TOKENS:
                totals["refused"] += 1
                refuse(self, f"Requested {sum(counts)} tokens, max {MAX_REQUEST_TOKENS} tokens per request")
                return
            dimensions = int(request.get("dimensions") or 1024)
            data = []
            for index, (text, count) in enumerate(zip(texts, counts)):
                seed = (len(text) * 31 + count) % 1009
                vector = [((seed + i * 17) % 101) / 101.0 + 0.001 for i in range(dimensions)]
                data.append({"object": "embedding", "index": index, "embedding": vector})
            body = json.dumps(
                {"object": "list", "data": data, "model": model, "usage": {"prompt_tokens": sum(counts), "total_tokens": sum(counts)}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(json.dumps(totals), file=sys.stderr, flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--model", default="text-embedding-3-large")
    args = parser.parse_args()
    serve(args.port, args.model)
