# Cloud device events

Deployment notifications are persisted transactionally in `aios.db`, projected
to the notification service, and exposed through the notification list,
dismiss, and SSE routes. Every SSE connection begins with an active-notification
snapshot before live bounded-queue events, so reconnecting clients can
resynchronize without relying on an in-memory replay buffer.

The cloud receiver is staged and disabled by default. Do not set
`AIOS_CLOUD_DEVICE_EVENTS_ENABLED=true` against the current `/ws/device`
implementation: that socket is the single-client request/response command relay,
not a deployment-event stream, and it does not understand `notification.ack`.
Enable the receiver only after aios-cloud provides a separate durable event
endpoint or a versioned multiplexed protocol with replay and acknowledgement.

The checked-out desktop also does not consume these routes yet. Its BoxClient
notification methods and startup subscription remain stubs, and its expected
event envelope/names must be reconciled with the server contract before this is
an end-to-end user-visible feature.
