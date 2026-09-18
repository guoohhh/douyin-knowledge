"""Adapter package: concrete implementations of AI provider protocols.

Each adapter module wraps a vendor SDK (OpenAI, Anthropic, etc.) to implement
the capability protocols defined in ai.providers.

V1 adapters:
- openai_adapter: OpenAI GPT-4o, Whisper, embeddings

Future adapters could include:
- anthropic_adapter: Claude models
- azure_adapter: Azure OpenAI
- local_adapter: Ollama, LocalAI for offline processing
"""
