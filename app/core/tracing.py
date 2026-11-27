from contextvars import ContextVar

# Set per HTTP request so audit events carry the trace ID without widening ports.
current_trace_id: ContextVar[str | None] = ContextVar("current_trace_id", default=None)
