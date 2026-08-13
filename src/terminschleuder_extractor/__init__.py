"""terminschleuder-extractor: an LLM-based extractor for the terminschleuder ingestion API.

Fetches due sources from the backend, scrapes each page, and asks an
OpenAI-compatible model (local Ollama or any other provider) to return events
as structured JSON, which is validated and submitted back as pending
observations. Container-first; also runnable from a host venv for tests.
"""

__version__ = "0.1.0"