"""Shared deployment endpoints for embedding and RAG applications.

Keep these separate from OLLAMA_HOST, which also configures summary checkpoints.
Applications use these endpoints explicitly so a stale shell or .env host cannot
silently send embedding requests to the generation server.
"""

OLLAMA_EMBED_HOST = "http://172.27.74.49:11434"
OLLAMA_RAG_HOST = "http://172.27.74.16:11434"
