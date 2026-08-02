# VTS pod topology — operator commands

> **NOT YET LIVE.** This document describes the topology **after** the
> planned cutover to the single-pod deployment (vts-0pg). At the time of
> writing, the cutover has **not** happened yet: the host still runs the old
> two-unit topology (`vts-webapi.service` / `vts-worker.service`), not
> `vts.service`. Do not follow the commands below against the current
> production host until the cutover is complete and this note is removed.

VTS runs as a single podman pod (`vts`) played by `vts.service` from
`/opt/vts/vts.yaml`. Containers inside it are `vts-webapi` and `vts-worker`;
migrations run once per start in the `vts-migrate` initContainer.

| Task | Command |
|---|---|
| Deploy / restart everything | `sudo systemctl restart vts` |
| Restart only the worker | `sudo systemctl start vts-worker-restart` |
| Restart only the web API | `sudo systemctl start vts-webapi-restart` |
| Stop the worker for maintenance | `sudo podman stop vts-worker` |
| Bring it back | `sudo podman start vts-worker` |
| Logs (one container) | `sudo podman logs -f vts-worker` |
| Migration output of the last start | `sudo podman logs vts-migrate` |
| Pod state | `sudo podman pod ps` / `sudo podman ps --pod` |

## Configuration

`/opt/vts/config/vts.env` is the single source of truth for runtime
configuration. `podman kube play` has no `--env-file` option (only
`--configmap`), so `vts.service` re-renders `/opt/vts/vts-configmap.yaml`
from `vts.env` on every start.

The rendered ConfigMap file must never be hand-edited — it is overwritten on
every start of `vts.service` — and must never be copied into the repo: it
contains secrets and is written mode `600`.

To change configuration: edit `vts.env`, then `sudo systemctl restart vts`.

Notes:

- Restarting the worker requeues whatever it was processing: in-flight tasks go
  back to `queued` and re-run from scratch. Prefer `vts-webapi-restart` when
  only the API is misbehaving.
- The two single-container restart units (`vts-worker-restart.service`,
  `vts-webapi-restart.service`) use `Requisite=vts.service` (not `Requires=`):
  if the pod is down, starting one of these fails immediately and leaves the
  pod alone, instead of starting it as a side effect.
- A container stopped by hand is NOT resurrected by `restartPolicy: Always`
  (that policy covers crashes, not deliberate stops). The pod shows `Degraded`
  until you start it again — the other container keeps serving.
- The diarization sidecar is a separate unit (`vts-diarization.service`) on
  purpose: its image has its own build pipeline, it must not see app secrets,
  and it is optional. It is unaffected by pod restarts.
- Rollback to the old two-unit topology: `sudo systemctl disable --now vts`,
  then re-enable `vts-webapi.service` and `vts-worker.service` (kept in git
  history if already deleted).
