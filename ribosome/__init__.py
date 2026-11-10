"""text generator with llm magic

Modules:

- `ribosome.preprocessing.ocr.audit_repair`: Validate structured OCR layout sidecars without making OCR or network calls, and retain actionable region-level findings for a later repair pass.
- `ribosome.preprocessing.ocr.baidu_unlimited`: Recursively parse PDF pages, Office documents, and images into Markdown with a remote [Baidu Unlimited-OCR](https://github.com/baidu/Unlimited-OCR) vLLM service."""

__version__ = "0.0.1"
