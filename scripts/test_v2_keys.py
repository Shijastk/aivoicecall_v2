"""
scripts/test_v2_keys.py
Standalone script to quickly verify if Shunya and Azure API keys are valid.
"""

import asyncio
import os
import aiohttp
from dotenv import load_dotenv

# Load credentials from the .env file
load_dotenv()

async def test_shunya_key():
    """Tests the Shunya Labs Indian English TTS API key."""
    api_key = os.getenv("SHUNYA_API_KEY")
    if not api_key:
        print("❌ ERROR: SHUNYA_API_KEY is missing in the .env file.")
        return

    print("Testing Shunya Labs API Key...")
    url = "https://tts.shunyalabs.ai/v1/audio/speech"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "zero-indic",
        "input": "Hello, this is a test to verify the API key.",
        "voice": "Sunita",
        "language": "en",
        "response_format": "pcm"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 200:
                    print("✅ SUCCESS: Shunya API Key is VALID! (Audio data received)")
                elif response.status in [401, 403]:
                    print("❌ FAILED: Shunya API Key is INVALID or EXPIRED (401 Unauthorized)")
                else:
                    error_text = await response.text()
                    print(f"❌ FAILED: Shunya API returned status {response.status} - {error_text}")
    except Exception as e:
        print(f"❌ FAILED: Connection error to Shunya API - {e}")


async def test_azure_key():
    """Tests the Microsoft Azure Malayalam TTS API key."""
    api_key = os.getenv("AZURE_SPEECH_KEY")
    region = os.getenv("AZURE_SPEECH_REGION", "centralindia")
    
    if not api_key:
        print("❌ ERROR: AZURE_SPEECH_KEY is missing in the .env file.")
        return

    print("Testing Microsoft Azure API Key...")
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "raw-16khz-16bit-mono-pcm"
    }
    ssml = (
        "<speak version='1.0' xml:lang='ml-IN'>"
        "<voice name='ml-IN-SobhanaNeural'>നമസ്കാരം, ഇതൊരു API ടെസ്റ്റ് ആണ്.</voice>"
        "</speak>"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=ssml) as response:
                if response.status == 200:
                    print("✅ SUCCESS: Azure API Key is VALID! (Audio data received)")
                elif response.status in [401, 403]:
                    print("❌ FAILED: Azure API Key is INVALID or EXPIRED (401 Unauthorized)")
                else:
                    error_text = await response.text()
                    print(f"❌ FAILED: Azure API returned status {response.status} - {error_text}")
    except Exception as e:
        print(f"❌ FAILED: Connection error to Azure API - {e}")


async def main():
    print("=== V2 TTS API KEY VERIFICATION ===\n")
    await test_shunya_key()
    print("-" * 50)
    await test_azure_key()
    print("\n=== VERIFICATION COMPLETE ===")

if __name__ == "__main__":
    asyncio.run(main())