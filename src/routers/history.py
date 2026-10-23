"""History management router: correlated logs across services and history retrieval."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from src.models.history import HistoryStore
from src.services.log_aggregator import LogAggregator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/history", tags=["History"])


@router.get("/{history_id}/correlated-logs")
async def get_correlated_logs(history_id: int, request: Request) -> dict[str, Any]:
    """Retrieve chronologically aggregated and correlated logs across services
    for a history entry.
    """
    history_store: HistoryStore | None = getattr(request.app.state, "history", None)
    if history_store is None:
        raise HTTPException(status_code=500, detail="History store is not initialized")

    record = await history_store.get(history_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"History item {history_id} not found")

    settings = getattr(request.app.state, "settings", None)
    aggregator = LogAggregator(settings=settings, history_store=history_store)
    return await aggregator.aggregate_for_record(record)
