# Product Requirements Document (PRD): Real-Time AI Speech Mentor (v2 Web Architecture)

## 1. Executive Summary & Core Objective
The v2 architecture transitions the AI Speech Mentor from a PSTN telephony-based system (using virtual numbers) to a direct, browser-based real-time web application. This update connects users directly to a custom 3D animated mentor character. The primary objective is to evaluate and improve the user's professional English communication skills in real-time, utilizing JSON-structured Language Model outputs for immediate barge-in corrections. Malayalam is supported strictly as a fallback language for conceptual explanations. 

To eliminate the recurring costs of virtual numbers and reduce latency, the v2 pipeline completely bypasses carrier routing. Instead, it utilizes WebSockets for high-definition, low-latency, two-way audio streaming directly to the frontend interface.

---

## 2. Strict Constraints: What NOT to Do
To guarantee zero regression in the currently stable system, the following constraints are absolute:

*   **Do Not Modify Telephony Carriers:** Files such as `shuo/carrier/vobiz.py` and `shuo/carrier/twilio.py` must remain completely untouched[cite: 2]. v2 will not use them.
*   **Do Not Modify the v0 Player:** The 20ms pacing loop in `shuo/services/player.py` must remain unchanged[cite: 2]. v2 will use a separate, un-paced direct stream to the browser.
*   **Do Not Alter the Core State Machine:** The pure state machine logic in `shuo/state.py` must be reused, not rewritten[cite: 2].
*   **Do Not Mix Languages Mid-Sentence:** To prevent latency spikes and unnatural prosody, code-switching within a single generated sentence is prohibited. A single session will route to one primary Text-to-Speech (TTS) engine.

---

## 3. What TO Do: The v2 Implementation Plan
The v2 update requires building an isolated pipeline that shares the AI logic of v0 but utilizes a separate transport layer.

### 3.1. Isolated Directory Structure
Create a dedicated folder for all v2 integrations. This ensures the production v1 endpoints remain safe.

*   `shuo/v2/`
*   `shuo/v2/api_v2.py` (New WebSocket routing endpoints)
*   `shuo/v2/agent_v2.py` (The modified agent utilizing the JSON evaluative loop)
*   `shuo/v2/services/`
*   `shuo/v2/services/tts_router.py` (Traffic controller for TTS engines)
*   `shuo/v2/services/tts_shunya.py` (Shunya Labs English TTS implementation)
*   `shuo/v2/services/tts_azure.py` (Microsoft Azure Malayalam TTS implementation)

### 3.2. Direct WebSocket Transport
Instead of exposing endpoints for Vobiz, create `ws://` endpoints in `api_v2.py`. 
*   The frontend will capture microphone audio via the `MediaRecorder` API and stream binary data to the backend.
*   The backend will stream generated audio chunks directly back to the frontend without the artificial 20ms pacing delay[cite: 2], allowing the browser's Web Audio API to handle buffer management natively.

### 3.3. Language-Based Session Routing
To maintain ultra-low latency, the language mode (English or Malayalam) is determined at the start of the session.
*   If the session is English, `tts_router.py` strictly loads the Shunya Labs TTS engine.
*   If the session requires Malayalam fallback, `tts_router.py` loads the Azure TTS engine.
*   Both engines must format their output to a standard sample rate (e.g., 16kHz PCM or 8kHz mu-law) before sending it down the socket.

### 3.4. JSON Evaluative Logic (The Barge-In System)
The `agent_v2.py` will force the Groq Large Language Model (LLM) to output strict JSON[cite: 1, 2].
*   The LLM must evaluate the user's grammar, filler word usage, and professional tone[cite: 1].
*   If `evaluation_status: false` is detected in the streaming JSON token chunks, the backend immediately triggers a barge-in event, halting the user and streaming the `intervention_text` via TTS to correct them[cite: 1].

### 3.5. Frontend Animation Control (State-Driven)
The backend does not compute animations. It only emits state strings over the WebSocket.
*   **State: "listening"**: Triggered by Voice Activity Detection (VAD) when the user speaks[cite: 1]. Frontend plays an idle/nodding 3D animation.
*   **State: "thinking"**: Triggered when the LLM is computing the Time-to-First-Token. Frontend plays a brief thinking animation.
*   **State: "talking"**: Triggered when audio begins streaming. The frontend Web Audio API analyzes audio frequencies to drive the 3D character's lip-sync (Blendshapes/Visemes) in real-time.

---

## 4. Required API Keys & Environment Variables
The `.env` file must be updated to support the v2 services.

*   `GROQ_API_KEY`: For ultra-fast Llama-3.3-70b JSON inference[cite: 2].
*   `DEEPGRAM_API_KEY`: For real-time streaming Speech-to-Text (STT) and Voice Activity Detection (VAD)[cite: 2].
*   `SHUNYA_API_KEY`: For the primary Indian-English TTS engine.
*   `AZURE_SPEECH_KEY` & `AZURE_SPEECH_REGION`: For the Malayalam fallback TTS engine.

---

## 5. Latency Architecture & Expectations
By removing the telecom carrier hops and eliminating the 20ms pacing constraint[cite: 2], the system latency is optimized exclusively for processing speed.

*   **VAD & STT Processing**: ~100ms
*   **Groq LLM (Time to First Token)**: ~150ms
*   **TTS Generation (Shunya/Azure)**: ~100ms
*   **Expected Total Server-Side Turn Latency**: **~350ms**

This guarantees a highly responsive, conversational experience identical to or faster than the existing v0 engine.