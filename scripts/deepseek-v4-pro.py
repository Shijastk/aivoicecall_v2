import time
import sys
from openai import OpenAI

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# Initialize the client with the NVIDIA API base URL
client = OpenAI(
  base_url="https://integrate.api.nvidia.com/v1",
  api_key="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" 
)

print("Sending request to NVIDIA API using the 'Flash' model...\n")

# Start the timer
start_time = time.time()

try:
    # Make the API call using the Flash model
    response = client.chat.completions.create(
      model="deepseek-ai/deepseek-v4-flash",  # Changed to the faster, available model
      messages=[
          {"role": "user", "content": "Hello! This is a speed test. Please reply quickly."}
      ],
      temperature=1,
      top_p=0.95,
      max_tokens=100, 
      stream=False # Still False, as we are timing the full response
    )
    
    # Stop the timer immediately after getting the response
    total_time = time.time() - start_time
    
    print("✅ Success! Here is the response:\n")
    print("-" * 40)
    print(response.choices[0].message.content)
    print("-" * 40)
    
    print(f"\n⏱️ Total time taken: {total_time:.2f} seconds")

except Exception as e:
    # Stop the timer if it fails
    elapsed = time.time() - start_time
    print(f"\n[ERROR] The API call failed: {e}")
    print(f"[TIME] Elapsed before failure: {elapsed:.2f} seconds")