import os
import time

from flask import Flask, Response, g, jsonify, render_template, request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

app = Flask(__name__)
REQUEST_COUNT = Counter(
    "flask_http_requests_total",
    "Total HTTP requests handled by the Flask application.",
    ("method", "endpoint", "status"),
)
REQUEST_LATENCY = Histogram(
    "flask_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("endpoint",),
)

trace_provider = TracerProvider(
    resource=Resource.create(
        {
            "service.name": "flask-app",
            "service.version": os.getenv("APP_VERSION", "dev"),
            "deployment.environment.name": os.getenv("APP_ENVIRONMENT", "local"),
        }
    )
)
otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
if otlp_endpoint:
    trace_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
    )
trace.set_tracer_provider(trace_provider)
FlaskInstrumentor().instrument_app(app)


@app.before_request
def start_request_timer():
    g.request_started_at = time.perf_counter()


@app.after_request
def record_request_metrics(response):
    if request.endpoint != "metrics":
        endpoint = request.endpoint or "unmatched"
        REQUEST_COUNT.labels(request.method, endpoint, str(response.status_code)).inc()
        REQUEST_LATENCY.labels(endpoint).observe(
            time.perf_counter() - g.request_started_at
        )
    return response


@app.get("/")
def index():
    in_kubernetes = bool(os.getenv("KUBERNETES_SERVICE_HOST"))
    return render_template(
        "index.html",
        app_version=os.getenv("APP_VERSION", "dev"),
        environment=os.getenv("APP_ENVIRONMENT", "local"),
        kubernetes_status=os.getenv(
            "KUBERNETES_STATUS", "Connected" if in_kubernetes else "Local only"
        ),
    )


@app.get("/health")
def health():
    return jsonify(status="healthy")


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)