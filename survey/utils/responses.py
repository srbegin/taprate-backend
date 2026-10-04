"""
Shared response helpers for TapRate API views.
"""


def list_response(items, **meta):
    """
    Return the canonical TapRate list envelope:

        { "items": [...], "meta": { "count": N, ...extra } }

    Usage:
        return Response(list_response(serializer.data))
        return Response(list_response(data, limit=10, at_limit=False))

    Rules:
      - items  — always an array, never null
      - meta   — always an object; count is always present
      - extra kwargs are merged into meta (limit, at_limit, page, page_size, etc.)
    """
    return {
        "items": items,
        "meta": {
            "count": len(items),
            **meta,
        },
    }