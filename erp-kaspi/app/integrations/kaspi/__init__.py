"""Kaspi API clients — skeleton with an abstract transport (OQ-003).

The official Kaspi Shop / Pay API specification is not available
(`BLOCKED — EXTERNAL API SPECIFICATION REQUIRED`). Per §94 no endpoint
paths, request or response shapes are invented: the transport is abstract,
every byte received is stored as a raw JSONB payload (§26), and real
calls are made only through a transport implementation supplied at
construction time (the real HTTP transport lands together with the
verified spec).
"""
