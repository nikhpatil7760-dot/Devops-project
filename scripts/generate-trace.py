from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
trace.set_tracer_provider(provider)

exporter = OTLPSpanExporter(
    endpoint="http://localhost:4317",
    insecure=True
)

provider.add_span_processor(BatchSpanProcessor(exporter))

tracer = trace.get_tracer("devops-lab")

with tracer.start_as_current_span("podinfo-test-operation") as span:
    span.set_attribute("service.name", "podinfo")
    span.set_attribute("environment", "local")
    print("Trace generated successfully")

provider.shutdown()
