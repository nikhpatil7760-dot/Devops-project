import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
provider = TracerProvider(
    resource=Resource.create(
        {
            "service.name": "flask-app-trace-demo",
            "deployment.environment.name": os.getenv("APP_ENVIRONMENT", "local"),
        }
    )
)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("devops-platform.trace-demo")
with tracer.start_as_current_span("deployment-check-demo") as span:
    span.set_attribute("app.route", "/health")
    span.set_attribute("demo.kind", "manual-trace")

flushed = provider.force_flush(timeout_millis=10000)
provider.shutdown()
if not flushed:
    raise SystemExit("Trace export did not flush; verify the OTLP collector is reachable.")
print(f"Trace exported to {endpoint}")