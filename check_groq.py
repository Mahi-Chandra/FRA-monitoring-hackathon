import requests, os
from dotenv import load_dotenv
load_dotenv()

api_key = os.environ.get("GROQ_API_KEY")
print("Key found:", bool(api_key), "starts with:", api_key[:8] if api_key else None)

resp = requests.get(
    "https://api.groq.com/openai/v1/models",
    headers={"Authorization": f"Bearer {api_key}"}
)
print("Status:", resp.status_code)
print(resp.json())