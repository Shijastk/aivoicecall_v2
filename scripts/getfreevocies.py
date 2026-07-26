import os
import sys
import time
import logging
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.core.api_error import ApiError

# Configure logging
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s | %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def verify_and_listen_to_voices():
    """
    Authenticates with the ElevenLabs API, tests each voice for actual synthesis
    permissions on the current plan, and provides a preview URL to hear them.
    """
    load_dotenv()
    
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        logging.error("CRITICAL: ELEVENLABS_API_KEY environment variable not found. Terminating.")
        sys.exit(1)

    try:
        client = ElevenLabs(api_key=api_key)
        logging.info("Fetching voices from ElevenLabs...")
        
        response = client.voices.get_all()
        available_voices = response.voices
        
        if not available_voices:
            logging.warning("No voices found associated with this API key.")
            return

        logging.info("Testing each voice to confirm actual API access... (This may take 10-15 seconds)")

        print("\n" + "="*140)
        print(f"{'VOICE NAME':<25} | {'VOICE ID':<22} | {'ACCENT':<15} | {'PREVIEW URL (CLICK TO HEAR)'}")
        print("="*140)
        
        usable_voices_count = 0

        for voice in available_voices:
            v_name = getattr(voice, 'name', 'Unknown Name')
            v_id = getattr(voice, 'voice_id', 'Unknown ID')
            
            labels = getattr(voice, 'labels', {}) or {}
            accent = labels.get('accent', 'N/A')
            
            # Get the preview URL so you can listen to it
            preview_url = getattr(voice, 'preview_url', 'No preview available')
            
            try:
                # REAL CONFIRMATION: Attempt to synthesize one character.
                # If your current plan blocks this voice via API, it will throw an ApiError here.
                list(client.text_to_speech.convert(
                    voice_id=v_id,
                    output_format="mp3_44100_128",
                    text="A"
                ))
                
                # If successful, it means the API allows this voice. Print it.
                clean_name = (v_name[:22] + '...') if len(v_name) > 25 else v_name
                print(f"{clean_name:<25} | {v_id:<22} | {accent:<15} | {preview_url}")
                usable_voices_count += 1
                
                # Small delay to avoid hitting free-tier API rate limits
                time.sleep(0.5)
                
            except ApiError:
                # The API blocked it (due to Free plan restrictions for this specific voice)
                pass

        print("="*140)
        print(f"Total VERIFIED Usable Voices Retrieved: {usable_voices_count}\n")

    except Exception as e:
        logging.error(f"An unexpected system error occurred: {str(e)}")

if __name__ == "__main__":
    verify_and_listen_to_voices()