"""The ids of the components basis builds around every page, which both phases after 14.5 build against: the data
lane's loads and failures write to them, and the display lane's shell restyles and reads them. Each is fixed, so a
page, a callback and a test name it by its constant.

- `NOTIFICATIONS`: DMC's `NotificationContainer`. A callback, or the app's `on_error` (`basis.dash.errors`), shows a
  notification by setting its `sendNotifications` prop to a list of notifications, each a dict of DMC's
  `Notification` props (`id`, `title`, `message`, `color`, `autoClose` and the rest), as
  `set_props(NOTIFICATIONS, {"sendNotifications": [...]})`; `hideNotifications`, a list of ids, hides them.
- `STATUS`: a `dcc.Store` whose `data` is a dict by dataset name, each value what the page knows of that dataset's
  last load: `{"loaded_at": ISO 8601 UTC, "rows": N, "stale": BOOL, "params": {NAME: VALUE}}`, which each load sets
  through a `Patch` (`basis.dash.loads`). It starts as `{}`, and holds no rows: a dataset never travels to the browser
  in a store."""

from typing import Final

NOTIFICATIONS: Final = "basis-notifications"
STATUS: Final = "basis-status"
