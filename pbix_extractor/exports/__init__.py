"""Client-ready export formats.

Each module here has its own third-party dependency (openpyxl for
Excel, reportlab for PDF) and is imported lazily by callers, so the core
extraction package keeps its no-dependency property.
"""