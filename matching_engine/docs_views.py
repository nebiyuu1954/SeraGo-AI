"""
Swagger UI and ReDoc views for SeraGo AI Matching Engine.

Serves the hand-crafted OpenAPI 3.0 schema from schema.py
with Swagger UI and ReDoc frontends.
"""

import json

from django.http import HttpResponse
from django.views.decorators.http import require_GET

from .schema import SCHEMA


SWAGGER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>SeraGo AI — API Docs</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
    <style>
        html { box-sizing: border-box; overflow-y: scroll; }
        *, *:before, *:after { box-sizing: inherit; }
        body { margin: 0; background: #fafafa; }
        .topbar { display: none; }
    </style>
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
        SwaggerUIBundle({
            spec: __SCHEMA_JSON__,
            dom_id: '#swagger-ui',
            deepLinking: true,
            presets: [
                SwaggerUIBundle.presets.apis,
                SwaggerUIBundle.SwaggerUIStandalonePreset
            ],
            layout: "BaseLayout"
        });
    </script>
</body>
</html>"""

REDOC_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>SeraGo AI — API Docs (ReDoc)</title>
    <link rel="stylesheet" href="https://unpkg.com/redoc@2/bundles/redoc.standalone.css">
</head>
<body>
    <redoc spec-url="/api/schema/"></redoc>
    <script src="https://unpkg.com/redoc@2/bundles/redoc.standalone.js"></script>
</body>
</html>"""


@require_GET
def swagger_ui(request):
    """Serve Swagger UI with embedded schema."""
    schema_json = json.dumps(SCHEMA, ensure_ascii=False)
    html = SWAGGER_UI_HTML.replace("__SCHEMA_JSON__", schema_json)
    return HttpResponse(html, content_type="text/html")


@require_GET
def redoc_ui(request):
    """Serve ReDoc with schema URL."""
    return HttpResponse(REDOC_HTML, content_type="text/html")


@require_GET
def schema_json_view(request):
    """Serve the OpenAPI schema as JSON."""
    return HttpResponse(
        json.dumps(SCHEMA, ensure_ascii=False, indent=2),
        content_type="application/json",
    )
