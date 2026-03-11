import asyncio
from typing import Dict, Any

# In-memory execution tracking
# Structure: { exec_id: { status, action_queue, response_event, etc. } }
EXECUTIONS: Dict[str, Any] = {}

def mark_execution(exec_id: str, **kwargs):
    """Update execution metadata in the global store."""
    if exec_id not in EXECUTIONS:
        EXECUTIONS[exec_id] = {
            "status": "queued",
            "action_queue": [],
            "response_event": asyncio.Event(),
            "last_response": None
        }
    EXECUTIONS[exec_id].update(kwargs)

def get_execution(exec_id: str) -> Dict[str, Any]:
    """Retrieve execution data from the store."""
    return EXECUTIONS.get(exec_id)
