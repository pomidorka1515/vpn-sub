from typing import Literal, cast

from ..common import BaseService, SharedCoreResources
from .user.common import CommonUserService
from .bandwidth import BandwidthService

class LeaderboardService(BaseService):
    def __init__(
        self,
        res: SharedCoreResources,
        *,
        user_svc: CommonUserService,
        bandwidth_svc: BandwidthService
    ) -> None:
        super().__init__(res)
        self.user_svc: CommonUserService = user_svc
        self.bandwidth_svc: BandwidthService = bandwidth_svc
    def leaderboard(
        self,
        category: Literal["total", "monthly", "wl_monthly"],
        *,
        top_n: int = 0,
        use_displaynames: bool = False,
        flip: bool = False
    ) -> dict[str, int]:
        """
        Dict of top-n users by specified bandwidth type, in order.
        WARNING: polls all users info. this is blocking!

        Args:
            category: type of bandwidth to use.
            top_n: clamp the leaderboard to top-n users.
                If <= 0, returns a leaderboard with all users.
            use_displaynames: if True, uses display names instead of internal usernames.
                Useful when passing data into chart.leaderboard_chart().
            flip: if True, sorted in ascending order, otherwise descending.

        Returns:
            A dict: {"username": 1293720471, "second_place": 12493630},
            where key is the username and value is bandwidth in bytes.
        """
        raw: dict[str, int] = {}
        users = self.user_svc.list_users()
        if use_displaynames:
            display_users = [str(self.user_svc.user(user)["displayname"]) for user in users]
        else:
            display_users = users
        # display_users is what we use in dict keys
        # populate the raw data
        match category:
            case 'total':
                for user, display in zip(users, display_users):
                    total = self.bandwidth_svc.bandwidth(user).total
                    raw[display] = cast(int, total) or 0
            case 'monthly' | 'wl_monthly':
                for user, display in zip(users, display_users):
                    state = self.user_svc.user(user)
                    if category == "monthly":
                        limit = state["bw_limit_gb"]
                        used = state["bw_used"]
                    else:
                        limit = state["wl_limit_gb"]
                        used = state["wl_used"]
                    if limit == 0:
                        continue # skip users who dont have bandwidth
                    raw[display] = used

        sorted_items: list[tuple[str, int]] = sorted(raw.items(), key=lambda x: x[1], reverse=not flip)
        if top_n > 0:
            sorted_items = sorted_items[:top_n]
        return {k: v for k, v in sorted_items}
