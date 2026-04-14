from core.orchestration.runner import OrchestrationRunner
from backend.schemas import CourseWorkspace
from core.metrics.collector import trace_scope
import json
import os

def run():
    runner = OrchestrationRunner()
    
    with trace_scope() as trace:
        try:
            for chunk in runner.run_stream("LLM基础", "learn", "你好是什么", None):
                pass
        except Exception as e:
            print("Error", e)
            
    for e in trace.events:
        if e.get("type") == "llm_call":
            print(f"Call: stream={e.get('stream')}, prompt={e.get('prompt_tokens')}, comp={e.get('completion_tokens')}")
            
if __name__ == "__main__":
    run()