import strawberry
from strawberry.types import Info
from api.decorators import require_api_secret
import logging

logger = logging.getLogger(__name__)

@strawberry.type
class SchedulerMutations:
    @strawberry.mutation
    @require_api_secret
    def trigger_channel_status_update(self, info: Info) -> str:
        """Manually trigger the channel/ad status update job"""
        from api.scheduler import update_channel_status
        
        try:
            logger.info("Manually triggering channel status update via GraphQL mutation")
            update_channel_status()
            return "Channel status update triggered successfully"
        except Exception as e:
            logger.error(f"Failed to trigger channel status update: {str(e)}", exc_info=True)
            return f"Error: {str(e)}"
