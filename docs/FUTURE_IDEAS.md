# Jarvis Future Add-Ons & Ideas

This is a living document to track upcoming features, architectural shifts, and cool ideas for the Jarvis project.

## Planned Features
- **Home Assistant / MQTT Integration**: Connect Jarvis to the smart home network. Allow the LLM to trigger lights, smart plugs, and routines by outputting specific JSON commands that the orchestrator translates into MQTT messages.
- **Custom JARVIS Community Voice**: Currently using the high-quality British male voice (`en_GB-alan`), but a community-trained JARVIS model (`jgkawell/jarvis`) exists. We should eventually train or download a bespoke Marvel JARVIS voice.
- **Wake-Word Optimization**: Enhance `run_listener.sh` to use a more robust VAD (Voice Activity Detection) pipeline to prevent false positives when listening for the wake word.
- **Multi-User Profiles**: Add proper user accounts so the frontend can store separate histories for different household members.
- **Vector-based Semantic Search**: Replace SQLite FTS5 Keyword RAG with a dedicated vector database (like Chroma or FAISS) and a local embedding model (e.g. `all-MiniLM-L6-v2`). This provides true semantic understanding of memories rather than exact keyword matches. (Note: Embedding models require slightly more CPU/RAM resources per message to compute cosine similarities).
- **Real-Time Voice Streaming**: Stream Piper TTS audio bytes instantly to the browser as the LLM generates text, instead of waiting for the full generation to complete.
- **Function Calling & Tools**: Grant JARVIS the ability to execute local server commands, control IoT devices, or search the web by giving the LLM function definitions.
