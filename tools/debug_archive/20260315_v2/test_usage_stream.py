from core.llm.openai_compat import LLMClient
from core.metrics.collector import trace_scope
import json

client = LLMClient()
with trace_scope() as trace:
    gen = client.chat_stream([{"role": "user", "content": "1+1等于几"}], max_tokens=10)
    for c in gen:
        pass
    
print("Events:")
print(json.dumps(trace.events, indent=2, ensure_ascii=False))