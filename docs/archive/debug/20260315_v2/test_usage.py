import os
from openai import OpenAI

def _usage_tokens(response):
    usage = None
    if isinstance(response, dict):
        usage = response.get("usage")
    else:
        usage = getattr(response, "usage", None)
    if usage is None:
         return None, None
    if isinstance(usage, dict):
        p = usage.get("prompt_tokens")
        c = usage.get("completion_tokens")
    else:
        p = getattr(usage, "prompt_tokens", None)
        c = getattr(usage, "completion_tokens", None)
    return p, c

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "test"), base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))

messages = [{"role": "user", "content": "hello"}]
try:
    stream = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=messages,
        stream=True,
        stream_options={"include_usage": True}
    )
    for chunk in stream:
        print(chunk)
        p, c = _usage_tokens(chunk)
        if p is not None:
            print(f"Tokens: {p}, {c}")
except Exception as e:
    print(f"Error {e}")